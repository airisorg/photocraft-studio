//! Per-worker admission bounds protect the request-driven service from unbounded queues.
//! These are overload controls, not a substitute for provider-wide abuse protection.

use axum::{
    Json,
    body::{Body, Bytes},
    extract::{Request, State},
    http::{HeaderValue, Method, StatusCode, header},
    middleware::Next,
    response::{IntoResponse, Response},
};
use http_body::{Body as HttpBody, Frame, SizeHint};
use serde_json::json;
use std::{
    future::Future,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore},
    time::{Instant, Sleep},
};

pub(crate) struct Limits {
    ordinary: Arc<Semaphore>,
    authentication: Arc<Semaphore>,
    commits: Arc<Semaphore>,
    ordinary_timeout: Duration,
    authentication_timeout: Duration,
    commit_timeout: Duration,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            // Local 100-client trials reached 183 simultaneous requests; retain headroom
            // without allowing an unbounded queue behind the database pool.
            ordinary: Arc::new(Semaphore::new(256)),
            authentication: Arc::new(Semaphore::new(4)),
            commits: Arc::new(Semaphore::new(2)),
            ordinary_timeout: Duration::from_secs(15),
            authentication_timeout: Duration::from_secs(40),
            commit_timeout: Duration::from_secs(45),
        }
    }
}

/// Keep admission until the response stream ends, fails, or is dropped. The
/// absolute request deadline also applies to body polling; it is not restarted
/// when response headers become ready. Socket/provider limits remain separate.
struct AdmittedBody {
    inner: Option<Body>,
    permit: Option<OwnedSemaphorePermit>,
    deadline: Pin<Box<Sleep>>,
}

impl AdmittedBody {
    fn finish(&mut self) {
        self.inner = None;
        self.permit = None;
    }
}

impl HttpBody for AdmittedBody {
    type Data = Bytes;
    type Error = axum::Error;

    fn poll_frame(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        let this = self.get_mut();
        if this.inner.is_none() {
            return Poll::Ready(None);
        }
        if this.deadline.as_mut().poll(cx).is_ready() {
            this.finish();
            // Headers may already have been sent: terminate the stream instead
            // of pretending a truncated document is a successful response.
            return Poll::Ready(Some(Err(axum::Error::new(std::io::Error::new(std::io::ErrorKind::TimedOut, "Response body exceeded the request deadline")))));
        }
        let Some(inner) = this.inner.as_mut() else {
            return Poll::Ready(None);
        };
        let result = Pin::new(&mut *inner).poll_frame(cx);
        if matches!(&result, Poll::Ready(None | Some(Err(_)))) || inner.is_end_stream() {
            this.finish();
        }
        result
    }

    fn is_end_stream(&self) -> bool {
        self.inner.is_none()
    }

    fn size_hint(&self) -> SizeHint {
        self.inner.as_ref().map_or_else(|| SizeHint::with_exact(0), HttpBody::size_hint)
    }
}

fn failure(status: StatusCode, message: &str) -> Response {
    let mut response = (status, Json(json!({"error":message}))).into_response();
    response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response.headers_mut().insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
    response
}

pub(crate) async fn admit(State(limits): State<Arc<Limits>>, request: Request, next: Next) -> Response {
    let path = request.uri().path();
    // Health checks stay independent of a saturated API lane.
    if path == "/healthz" {
        return next.run(request).await;
    }
    let authentication = path == "/auth/callback" || (path == "/auth/confirm" && request.method() == Method::POST);
    let commit = path.starts_with("/api/uploads/") && path.ends_with("/commit");
    let (lane, timeout) = if authentication {
        (&limits.authentication, limits.authentication_timeout)
    } else if commit {
        (&limits.commits, limits.commit_timeout)
    } else {
        (&limits.ordinary, limits.ordinary_timeout)
    };
    // Do not queue arbitrary numbers of bodies/futures behind a small database pool.
    let Ok(permit) = lane.clone().try_acquire_owned() else {
        return failure(StatusCode::SERVICE_UNAVAILABLE, "This service is busy. Your local work is safe; retry shortly.");
    };
    let deadline = Instant::now() + timeout;
    match tokio::time::timeout_at(deadline, next.run(request)).await {
        Ok(response) => response.map(|body| {
            if body.is_end_stream() {
                drop(permit);
                body
            } else {
                Body::new(AdmittedBody { inner: Some(body), permit: Some(permit), deadline: Box::pin(tokio::time::sleep_until(deadline)) })
            }
        }),
        Err(_) => failure(StatusCode::GATEWAY_TIMEOUT, "This request took too long. Check the saved version before retrying; your local work is unchanged."),
    }
}

/// Bound total pool fanout explicitly; larger values require measured provider capacity.
pub(crate) fn pool_size(value: Option<&str>) -> Result<u32, &'static str> {
    match value {
        None => Ok(5),
        Some(value) => value.parse::<u32>().ok().filter(|value| (1..=32).contains(value)).ok_or("PHOTOCRAFT_DB_POOL_SIZE must be an integer from 1 to 32"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{Router, body::Body, middleware, routing::get};
    use tower::ServiceExt;

    #[derive(Clone, Copy)]
    enum Terminal {
        End,
        Error,
        Pending,
    }

    struct TestStream {
        prefix_sent: bool,
        terminal: Terminal,
    }

    impl HttpBody for TestStream {
        type Data = Bytes;
        type Error = std::io::Error;

        fn poll_frame(mut self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            if !self.prefix_sent {
                self.prefix_sent = true;
                return Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(b"native document prefix")))));
            }
            match self.terminal {
                Terminal::End => Poll::Ready(None),
                Terminal::Error => Poll::Ready(Some(Err(std::io::Error::other("synthetic body failure")))),
                Terminal::Pending => Poll::Pending,
            }
        }
    }

    fn streaming_router(limits: Arc<Limits>, terminal: Terminal) -> Router {
        Router::new()
            .route("/api/stream", get(move || async move { Body::new(TestStream { prefix_sent: false, terminal }) }))
            .route("/healthz", get(|| async { StatusCode::NO_CONTENT }))
            .layer(middleware::from_fn_with_state(limits, admit))
    }

    fn request(path: &str) -> Request {
        Request::builder().uri(path).body(Body::empty()).unwrap()
    }

    async fn frame(body: &mut Body) -> Option<Result<Frame<Bytes>, axum::Error>> {
        std::future::poll_fn(|cx| Pin::new(&mut *body).poll_frame(cx)).await
    }

    fn test_router(limits: Arc<Limits>) -> Router {
        Router::new()
            .route("/api/test", get(|| async { StatusCode::NO_CONTENT }))
            .route("/auth/callback", get(|| async { StatusCode::NO_CONTENT }))
            .route("/healthz", get(|| async { StatusCode::NO_CONTENT }))
            .layer(middleware::from_fn_with_state(limits, admit))
    }

    #[test]
    fn pool_fanout_rejects_invalid_configuration() {
        assert_eq!(pool_size(None), Ok(5));
        assert_eq!(pool_size(Some("1")), Ok(1));
        assert_eq!(pool_size(Some("32")), Ok(32));
        for value in ["0", "33", "-1", "", "1000", "connection-secret", "1.5"] {
            assert!(pool_size(Some(value)).is_err());
        }
    }

    #[tokio::test]
    async fn saturated_authentication_cannot_starve_api_or_health_checks() {
        let limits = Arc::new(Limits::default());
        let held = limits.authentication.acquire_many(4).await;
        assert!(held.is_ok());
        let app = test_router(limits.clone());
        let auth = app.clone().oneshot(Request::builder().uri("/auth/callback").body(Body::empty()).unwrap()).await.unwrap();
        assert_eq!(auth.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(auth.headers().get(header::CACHE_CONTROL).unwrap(), "no-store");
        assert_eq!(auth.headers().get(header::RETRY_AFTER).unwrap(), "1");
        let api = app.clone().oneshot(Request::builder().uri("/api/test").body(Body::empty()).unwrap()).await.unwrap();
        assert_eq!(api.status(), StatusCode::NO_CONTENT);
        drop(api);
        let all_api = limits.ordinary.acquire_many(256).await;
        assert!(all_api.is_ok());
        let health = app.oneshot(Request::builder().uri("/healthz").body(Body::empty()).unwrap()).await.unwrap();
        assert_eq!(health.status(), StatusCode::NO_CONTENT);
    }

    #[tokio::test]
    async fn deadline_releases_capacity_and_does_not_report_success() {
        let limits = Arc::new(Limits { ordinary_timeout: Duration::from_millis(10), ..Limits::default() });
        let app = Router::new()
            .route(
                "/api/test",
                get(|| async {
                    tokio::time::sleep(Duration::from_secs(1)).await;
                    StatusCode::NO_CONTENT
                }),
            )
            .layer(middleware::from_fn_with_state(limits.clone(), admit));
        let response = app.oneshot(Request::builder().uri("/api/test").body(Body::empty()).unwrap()).await.unwrap();
        assert_eq!(response.status(), StatusCode::GATEWAY_TIMEOUT);
        assert_eq!(limits.ordinary.available_permits(), 256);
    }

    #[tokio::test]
    async fn retained_and_pending_response_bodies_hold_capacity_until_drop() {
        let limits = Arc::new(Limits { ordinary: Arc::new(Semaphore::new(1)), ..Limits::default() });
        let app = streaming_router(limits.clone(), Terminal::Pending);
        let response = app.clone().oneshot(request("/api/stream")).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(limits.ordinary.available_permits(), 0);
        // Retaining the headers without polling the body must not free a slot.
        assert_eq!(app.clone().oneshot(request("/api/stream")).await.unwrap().status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(app.clone().oneshot(request("/healthz")).await.unwrap().status(), StatusCode::NO_CONTENT);
        let mut body = response.into_body();
        assert_eq!(frame(&mut body).await.unwrap().unwrap().into_data().unwrap(), "native document prefix");
        assert!(tokio::time::timeout(Duration::from_millis(5), frame(&mut body)).await.is_err());
        assert_eq!(limits.ordinary.available_permits(), 0);
        drop(body);
        assert_eq!(limits.ordinary.available_permits(), 1);
        let next = app.oneshot(request("/api/stream")).await.unwrap();
        assert_eq!(next.status(), StatusCode::OK);
        drop(next);
        assert_eq!(limits.ordinary.available_permits(), 1);
    }

    #[tokio::test]
    async fn body_end_and_error_release_capacity_without_dropping_response() {
        for terminal in [Terminal::End, Terminal::Error] {
            let limits = Arc::new(Limits { ordinary: Arc::new(Semaphore::new(1)), ..Limits::default() });
            let app = streaming_router(limits.clone(), terminal);
            let mut body = app.oneshot(request("/api/stream")).await.unwrap().into_body();
            assert!(frame(&mut body).await.unwrap().is_ok());
            assert_eq!(limits.ordinary.available_permits(), 0);
            match terminal {
                Terminal::End => assert!(frame(&mut body).await.is_none()),
                Terminal::Error => assert!(frame(&mut body).await.unwrap().is_err()),
                Terminal::Pending => unreachable!(),
            }
            assert_eq!(limits.ordinary.available_permits(), 1);
            assert!(body.is_end_stream());
            assert!(frame(&mut body).await.is_none());
        }
    }

    #[tokio::test]
    async fn pending_body_deadline_terminates_stream_and_releases_capacity() {
        let limits = Arc::new(Limits { ordinary: Arc::new(Semaphore::new(1)), ordinary_timeout: Duration::from_millis(20), ..Limits::default() });
        let app = streaming_router(limits.clone(), Terminal::Pending);
        let mut body = app.clone().oneshot(request("/api/stream")).await.unwrap().into_body();
        assert!(frame(&mut body).await.unwrap().is_ok());
        assert_eq!(limits.ordinary.available_permits(), 0);
        let error = tokio::time::timeout(Duration::from_secs(1), frame(&mut body)).await.unwrap().unwrap().unwrap_err();
        assert!(error.to_string().contains("deadline"));
        assert_eq!(limits.ordinary.available_permits(), 1);
        assert!(body.is_end_stream());
        assert!(frame(&mut body).await.is_none());
        assert_eq!(app.oneshot(request("/api/stream")).await.unwrap().status(), StatusCode::OK);
    }
}

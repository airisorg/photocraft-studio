//! Email invitations use the app's Tofu-managed authentication mail service.
use super::*;
use axum::{Form, response::Html};

pub(crate) async fn invite(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>, Json(v): Json<Member>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, true, true).await?;
    let email = email(&v.email)?;
    if !matches!(v.role.as_str(), "view" | "edit") || email == a.email {
        return Err(bad("Choose another person and a view or edit role"));
    }
    if s.supabase.is_empty() || s.anon.is_empty() {
        return Err(bad("Email invitations are not configured yet"));
    }
    let pool = db(&s)?;
    let mut tx = pool.begin().await?;
    // Serialize the owner's rate limit, including concurrent requests and failed sends.
    query("SELECT id FROM photocraft.accounts WHERE id=$1 FOR UPDATE").bind(a.id).execute(&mut *tx).await?;
    let recent: i64 = scalar("SELECT count(*) FROM photocraft.invitation_deliveries WHERE sender_id=$1 AND created_at>now()-interval '1 hour'")
        .bind(a.id)
        .fetch_one(&mut *tx)
        .await?;
    let cooldown: bool = scalar("SELECT EXISTS(SELECT 1 FROM photocraft.invitation_deliveries WHERE email=$1 AND created_at>now()-interval '1 minute')")
        .bind(&email)
        .fetch_one(&mut *tx)
        .await?;
    if recent >= 20 || cooldown {
        return Err(ApiError(
            StatusCode::TOO_MANY_REQUESTS,
            "Please wait before sending another invitation. Limit: 20 per hour and one per recipient per minute.".into(),
        ));
    }
    let count: i64 = scalar("SELECT count(*) FROM photocraft.members WHERE project_id=$1").bind(id).fetch_one(&mut *tx).await?;
    if count >= 100 {
        return Err(bad("This project has reached 100 collaborators"));
    }
    query("INSERT INTO photocraft.members(project_id,email,role) VALUES($1,$2,$3) ON CONFLICT(project_id,email) DO UPDATE SET role=EXCLUDED.role")
        .bind(id)
        .bind(&email)
        .bind(&v.role)
        .execute(&mut *tx)
        .await?;
    let delivery = Uuid::new_v4();
    query("INSERT INTO photocraft.invitation_deliveries(id,project_id,sender_id,email) VALUES($1,$2,$3,$4)")
        .bind(delivery)
        .bind(id)
        .bind(a.id)
        .bind(&email)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    let sent = s
        .http
        .post(format!("{}/auth/v1/otp", s.supabase))
        .query(&[("redirect_to", format!("{}/auth/confirm", s.origin))])
        .header("apikey", &s.anon)
        .json(&json!({"email":email,"create_user":true}))
        .send()
        .await
        .is_ok_and(|r| r.status().is_success());
    let mut tx = pool.begin().await?;
    query("UPDATE photocraft.invitation_deliveries SET status=$2 WHERE id=$1")
        .bind(delivery)
        .bind(if sent { "sent" } else { "failed" })
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    if !sent {
        return Err(ApiError(
            StatusCode::BAD_GATEWAY,
            "Access was granted, but the email service did not confirm sending. Retry the invitation in a minute, or share the project link.".into(),
        ));
    }
    Ok(Json(json!({"ok":true,"message":"Sign-in invitation sent. After verifying this email address, the project appears in Shared with you."})))
}

fn email(value: &str) -> Result<String> {
    let value = text(value, 320)?.to_lowercase();
    let Some((local, domain)) = value.split_once('@') else {
        return Err(bad("Enter a valid email address"));
    };
    if local.is_empty()
        || local.len() > 64
        || domain.is_empty()
        || !domain.contains('.')
        || value.chars().any(|c| c.is_whitespace() || c.is_control())
        || domain.contains('@')
    {
        return Err(bad("Enter a valid email address"));
    }
    Ok(value)
}

#[derive(Deserialize)]
pub(crate) struct Confirmation {
    token_hash: String,
    #[serde(rename = "type")]
    kind: String,
}
fn valid(v: &Confirmation) -> bool {
    (16..=512).contains(&v.token_hash.len())
        && v.token_hash.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
        && matches!(v.kind.as_str(), "email" | "magiclink" | "signup" | "invite")
}

pub(crate) async fn confirm_page(Query(v): Query<Confirmation>) -> Result<Html<String>> {
    if !valid(&v) {
        return Err(bad("This sign-in link is invalid. Ask for a new invitation."));
    }
    // Opening the link does not consume it. Email scanners cannot burn a single-use token.
    Ok(Html(format!(
        r#"<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="strict-origin"><title>Join PhotoCraft Studio</title><style>body{{margin:0;background:#f7f7fa;color:#252331;font:16px system-ui;display:grid;place-items:center;min-height:100dvh}}main{{max-width:390px;padding:40px;margin:24px;background:white;border:1px solid #e8e7ed;border-radius:20px;box-shadow:0 12px 40px #2523310b}}h1{{font-size:28px;letter-spacing:-.8px;line-height:1.15}}p{{color:#676575;line-height:1.6}}button{{background:#7048dc;border:0;color:white;width:100%;font:600 15px system-ui;padding:14px;border-radius:10px;cursor:pointer}}button:focus-visible{{outline:3px solid #bba4fc;outline-offset:4px}}small{{display:block;margin-top:20px;color:#767282}}</style><main><b>PhotoCraft Studio</b><h1>Your next great idea starts here.</h1><p>Continue to verify your email and open your workspace. Projects shared with this address will be waiting for you.</p><form method="post" action="/auth/confirm"><input type="hidden" name="token_hash" value="{}"><input type="hidden" name="type" value="{}"><button type="submit">Continue to PhotoCraft</button></form><small>Only continue if you requested this sign-in or expected an invitation.</small><p><a href="https://trytofu.ai" rel="noreferrer">Hosted on Tofu</a></p></main></html>"#,
        v.token_hash, v.kind
    )))
}

pub(crate) async fn confirm(State(s): State<App>, Form(v): Form<Confirmation>) -> Result<Response> {
    if !valid(&v) {
        return Err(bad("This sign-in link is invalid. Ask for a new invitation."));
    }
    finish_sign_in(&s, &v.token_hash, &v.kind).await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn address_validation_and_token_html_are_bounded() {
        for address in ["", "@example.com", "a@", "a@b@c.com", "a b@c.com", "a\n@c.com"] {
            assert!(email(address).is_err());
        }
        assert_eq!(email(" Person@Example.com ").unwrap(), "person@example.com");
        for token in ["short", "\"><script>alert(1)</script>", "a&b________________"] {
            assert!(!valid(&Confirmation { token_hash: token.into(), kind: "email".into() }));
        }
        assert!(valid(&Confirmation { token_hash: "a".repeat(64), kind: "email".into() }));
    }
}

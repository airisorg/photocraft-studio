// Modified by the independent FZ2000 PhotoCraft Studio fork; see docs/fork-code-map.md.
//! The browser shell: web `Services`, drag-and-drop, and the eframe web runner.

use std::sync::{Arc, Mutex};

use photocraft_codecs::{ChannelLayout, EncodeOptions, Image};
use photocraft_doc::Document;
use photocraft_engine::Session;
use photocraft_ui_egui::theme::ThemeKind;
use photocraft_ui_egui::{PhotocraftApp, Services};
use wasm_bindgen::JsCast as _;

type Inbox = Arc<Mutex<Vec<(String, Vec<u8>)>>>;

/// Everything File › Open reads: PhotoCraft and Photoshop documents, flat images, and Photoshop
/// brushes (.abr) and gradients (.grd), which go to the preset libraries.
const OPEN_EXTS: &[&str] = &[
    "pcraft", "psd", "psb", "png", "jpg", "jpeg", "tif", "tiff", "webp", "gif", "bmp", "tga", "ico", "qoi", "exr", "hdr", "pbm", "pgm", "ppm", "pam", "pfm",
    "heic", "heif", "hif", "dng", "cr2", "cr3", "nef", "nrw", "arw", "pef", "orf", "rw2", "raf", "abr", "grd",
];
const CANVAS_ID: &str = "photocraft_canvas";

pub fn start() {
    eframe::WebLogger::init(log::LevelFilter::Info).ok();
    listen_unload();
    wasm_bindgen_futures::spawn_local(async {
        let Some(document) = web_sys::window().and_then(|w| w.document()) else {
            log::error!("no document");
            return;
        };
        let Some(canvas) = document.get_element_by_id(CANVAS_ID).and_then(|e| e.dyn_into::<web_sys::HtmlCanvasElement>().ok()) else {
            log::error!("missing <canvas id=\"{CANVAS_ID}\">");
            return;
        };
        let q = query();
        let force_cpu = q.contains("cpu");
        let mut options = eframe::WebOptions::default();
        photocraft_ui_egui::gpu_canvas::use_adapter_limits(&mut options.wgpu_options.wgpu_setup);
        if q.contains("webgl")
            && let eframe::egui_wgpu::WgpuSetup::CreateNew(create) = &mut options.wgpu_options.wgpu_setup
        {
            create.instance_descriptor.backends = eframe::wgpu::Backends::GL;
        } else {
            prefer_hardware_webgpu(&mut options).await;
        }
        let pen_target = canvas.clone();
        let result = eframe::WebRunner::new()
            .start(
                canvas,
                options,
                Box::new(move |cc| {
                    PhotocraftApp::setup_context(&cc.egui_ctx, ThemeKind::Pro);
                    let inbox: Inbox = Arc::default();
                    let mut app = PhotocraftApp::new(Session::new(), services(inbox.clone(), cc.egui_ctx.clone()));
                    let (control_tx, control_rx) = std::sync::mpsc::channel();
                    install_bridge(control_tx, cc.egui_ctx.clone());
                    app = app.with_control(control_rx);
                    if local_storage().and_then(|s| s.get_item(PREFS_KEY).ok().flatten()).is_none() {
                        app.session.edit_prefs(|p| p.interface.theme = photocraft_engine::prefs::Theme::Studio);
                    }
                    listen_pen(&pen_target, app.stylus.feed.clone());
                    app.set_theme(&cc.egui_ctx, ThemeKind::Pro);
                    if let Some(rs) = cc.wgpu_render_state.clone()
                        && !force_cpu
                        && app.session.prefs().performance.effective_rendering_mode() != photocraft_engine::prefs::RenderingMode::Cpu
                    {
                        log::info!("photocraft-web: wgpu backend {:?}", rs.adapter.get_info().backend);
                        app.set_wgpu(rs);
                    }
                    let cloud = crate::cloud::Cloud::new(&cc.egui_ctx);
                    Ok(Box::new(WebShell { app, inbox, cloud }))
                }),
            )
            .await;
        if let Some(el) = document.get_element_by_id("photocraft_loading") {
            match result {
                Ok(()) => el.remove(),
                Err(e) => {
                    el.set_text_content(None);
                    el.set_class_name("photocraft-startup-error");
                    for (tag, text) in [
                        ("strong", "Let’s get your workspace open"),
                        ("p", "PhotoCraft couldn’t start the graphics renderer. Try the compatible renderer, or reload if you’re already using it."),
                    ] {
                        if let Ok(child) = document.create_element(tag) {
                            child.set_text_content(Some(text));
                            let _ = el.append_child(&child);
                        }
                    }
                    if let Some(w) = web_sys::window()
                        && let Ok(href) = w.location().href()
                        && let Ok(url) = web_sys::Url::new(&href)
                        && let Ok(link) = document.create_element("a")
                    {
                        let search = url.search();
                        if !q.contains("webgl") {
                            url.set_search(&format!("{search}{}webgl", if search.is_empty() { "?" } else { "&" }));
                        }
                        link.set_text_content(Some(if q.contains("webgl") { "Reload PhotoCraft" } else { "Try the compatible renderer" }));
                        let _ = link.set_attribute("href", &url.href());
                        let _ = el.append_child(&link);
                    }
                    if let Ok(details) = document.create_element("details")
                        && let Ok(summary) = document.create_element("summary")
                        && let Ok(diagnostic) = document.create_element("pre")
                    {
                        summary.set_text_content(Some("Technical details"));
                        diagnostic.set_text_content(Some(&format!("{e:?}")));
                        let _ = details.append_child(&summary);
                        let _ = details.append_child(&diagnostic);
                        let _ = el.append_child(&details);
                    }
                }
            }
        }
    });
}

thread_local! {static UNSAVED:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};}
pub(crate) fn set_unsaved(value: bool) {
    UNSAVED.with(|v| v.set(value));
}
fn listen_unload() {
    use wasm_bindgen::closure::Closure;
    let callback = Closure::<dyn FnMut(web_sys::BeforeUnloadEvent)>::new(|e: web_sys::BeforeUnloadEvent| {
        if UNSAVED.with(|v| v.get()) {
            e.prevent_default();
            e.set_return_value("");
        }
    });
    if let Some(w) = web_sys::window() {
        if w.add_event_listener_with_callback("beforeunload", callback.as_ref().unchecked_ref()).is_ok() {
            callback.forget();
        }
    }
}

/// Same-origin automation seam, using the exact native control protocol. No network listener,
/// filesystem access or credentials: foreign origins cannot access this window property.
fn install_bridge(tx: std::sync::mpsc::Sender<photocraft_ui_egui::ControlRequest>, ctx: egui::Context) {
    use wasm_bindgen::closure::Closure;
    let f = Closure::<dyn FnMut(String, String) -> js_sys::Promise>::new(move |method: String, params: String| {
        let tx = tx.clone();
        let ctx = ctx.clone();
        wasm_bindgen_futures::future_to_promise(async move {
            let params = serde_json::from_str(&params).map_err(|_| wasm_bindgen::JsValue::from_str("Invalid command JSON"))?;
            let (req, rx) = photocraft_ui_egui::ControlRequest::new(method, params);
            tx.send(req).map_err(|_| wasm_bindgen::JsValue::from_str("Editor is unavailable"))?;
            ctx.request_repaint();
            for _ in 0..1800 {
                if let Ok(v) = rx.try_recv() {
                    return Ok(wasm_bindgen::JsValue::from_str(&v.to_string()));
                }
                gloo_timers::future::TimeoutFuture::new(16).await;
            }
            Err(wasm_bindgen::JsValue::from_str("Editor command timed out"))
        })
    });
    if let Some(w) = web_sys::window() {
        let _ = js_sys::Reflect::set(&w, &"photocraftCommand".into(), f.as_ref());
        f.forget();
    }
}

/// Pen pressure, tilt, twist and the eraser button from Pointer Events (eframe forwards none of them for pens) into
/// the app's stylus feed. The sample is kept through `pointerup` so the stroke's last points keep
/// their pressure; hovering, a mouse, or leaving the canvas clears it.
fn listen_pen(target: &web_sys::HtmlCanvasElement, feed: photocraft_ui_egui::stylus::StylusFeed) {
    use photocraft_ui_egui::stylus::PenSample;
    use wasm_bindgen::closure::Closure;
    for kind in ["pointerdown", "pointermove", "pointerup", "pointercancel", "pointerleave"] {
        let feed = feed.clone();
        let cb = Closure::<dyn FnMut(web_sys::PointerEvent)>::new(move |e: web_sys::PointerEvent| {
            let ty = e.type_();
            if ty == "pointerup" && e.pointer_type() == "pen" {
                return;
            }
            let pen = e.pointer_type() == "pen" && e.buttons() != 0 && ty != "pointercancel" && ty != "pointerleave";
            // W3C Pointer Events: `buttons` bit 5 (32) is the pen's eraser.
            let eraser = e.buttons() & 32 != 0;
            feed.set(pen.then(|| PenSample {
                pressure: e.pressure(),
                tilt_x: e.tilt_x() as f32,
                tilt_y: e.tilt_y() as f32,
                rotation: e.twist() as f32,
                eraser,
            }));
        });
        if target.add_event_listener_with_callback(kind, cb.as_ref().unchecked_ref()).is_ok() {
            cb.forget();
        }
    }
}

fn query() -> String {
    web_sys::window().and_then(|w| w.location().search().ok()).unwrap_or_default()
}

/// Wraps the app to read dropped files asynchronously (browsers can't read them synchronously,
/// so the app's own drop path can't handle them) and feed them through the inbox.
struct WebShell {
    app: PhotocraftApp,
    inbox: Inbox,
    cloud: crate::cloud::Cloud,
}

impl eframe::App for WebShell {
    fn logic(&mut self, ctx: &egui::Context, frame: &mut eframe::Frame) {
        self.cloud.handle_input(ctx);
        let dropped = ctx.input_mut(|i| std::mem::take(&mut i.raw.dropped_files));
        for f in dropped {
            let inbox = self.inbox.clone();
            let ctx = ctx.clone();
            wasm_bindgen_futures::spawn_local(async move {
                let name = f.path().file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_else(|| "dropped".into());
                match f.bytes_async().await {
                    Ok(bytes) => {
                        inbox.lock().unwrap_or_else(|e| e.into_inner()).push((name, bytes));
                        ctx.request_repaint();
                    }
                    Err(e) => log::error!("couldn't read dropped file {name}: {e}"),
                }
            });
        }
        // Native File > Open and browser drops share this inbox. Admit their documents
        // through the cloud adapter so a persisted native ID cannot reuse a closed binding.
        let arrived = std::mem::take(&mut *self.inbox.lock().unwrap_or_else(|e| e.into_inner()));
        for (name, bytes) in arrived {
            self.cloud.open_local(&mut self.app, &name, &bytes);
        }
        self.app.logic(ctx, frame);
        self.cloud.update(&mut self.app, ctx);
    }

    fn ui(&mut self, ui: &mut egui::Ui, frame: &mut eframe::Frame) {
        self.cloud.ui(&mut self.app, ui, frame);
    }
}

fn services(inbox: Inbox, ctx: egui::Context) -> Services {
    let open_inbox = inbox.clone();
    Services {
        import: Some(Box::new(|name: &str, bytes: &[u8]| photocraft_io::import(name, bytes).map(|r| (r.document, r.warnings)).map_err(|e| e.to_string()))),
        export: Some(Box::new(|doc: &Document, path: &str, settings: &photocraft_ui_egui::ExportSettings| {
            let mut opts = photocraft_io::ExportOptions::default();
            if let Some(q) = settings.jpeg_quality {
                opts.encode.jpeg_quality = q;
            }
            photocraft_io::export(doc, path, &opts).map(|r| (r.bytes, r.warnings)).map_err(|e| e.to_string())
        })),
        pick_open: Some(Box::new(move || {
            let inbox = open_inbox.clone();
            let ctx = ctx.clone();
            wasm_bindgen_futures::spawn_local(async move {
                let Some(file) = rfd::AsyncFileDialog::new().add_filter("All Formats", OPEN_EXTS).pick_file().await else {
                    return;
                };
                let bytes = file.read().await;
                inbox.lock().unwrap_or_else(|e| e.into_inner()).push((file.file_name(), bytes));
                ctx.request_repaint();
            });
            None
        })),
        // No save dialog on the web: the suggested name becomes the download name.
        pick_save: Some(Box::new(|suggested: &str| {
            let name = std::path::Path::new(suggested).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_else(|| suggested.to_string());
            Some(name)
        })),
        write: Some(Box::new(|path: &str, bytes: &[u8]| download(path, bytes))),
        encode_png: Some(Box::new(|w, h, rgba| {
            let img = Image::from_u8(w, h, ChannelLayout::Rgba, rgba.to_vec()).map_err(|e| e.to_string())?;
            photocraft_codecs::encode(&img, photocraft_codecs::Format::Png, &EncodeOptions::default()).map_err(|e| e.to_string())
        })),
        inbox: Some(inbox),
        // Preferences live in the browser's localStorage.
        load_prefs: Some(Box::new(|| local_storage()?.get_item(PREFS_KEY).ok().flatten())),
        save_prefs: Some(Box::new(|text: &str| local_storage().ok_or("no localStorage")?.set_item(PREFS_KEY, text).map_err(|e| format!("{e:?}")))),
        ..Default::default()
    }
}

const PREFS_KEY: &str = "photocraft.preferences";

fn local_storage() -> Option<web_sys::Storage> {
    web_sys::window()?.local_storage().ok().flatten()
}

/// Trigger a browser download of `bytes` named after the last component of `path`.
pub(crate) fn download(path: &str, bytes: &[u8]) -> Result<(), String> {
    let js = |e: wasm_bindgen::JsValue| format!("{e:?}");
    let name = std::path::Path::new(path).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_else(|| "photocraft".into());
    let window = web_sys::window().ok_or("no window")?;
    let document = window.document().ok_or("no document")?;
    let parts = js_sys::Array::of1(&js_sys::Uint8Array::from(bytes));
    let opts = web_sys::BlobPropertyBag::new();
    opts.set_type(mime_for(&name));
    let blob = web_sys::Blob::new_with_u8_array_sequence_and_options(&parts, &opts).map_err(js)?;
    let url = web_sys::Url::create_object_url_with_blob(&blob).map_err(js)?;
    let a: web_sys::HtmlAnchorElement = document.create_element("a").map_err(js)?.dyn_into().map_err(|_| "not an anchor")?;
    a.set_href(&url);
    a.set_download(&name);
    a.style().set_property("display", "none").map_err(js)?;
    let body = document.body().ok_or("no body")?;
    body.append_child(&a).map_err(js)?;
    a.click();
    a.remove();
    // Revoke after the click has been dispatched; the download keeps its own reference.
    let revoke = wasm_bindgen::closure::Closure::once_into_js(move || {
        web_sys::Url::revoke_object_url(&url).ok();
    });
    window.set_timeout_with_callback_and_timeout_and_arguments_0(revoke.unchecked_ref(), 10_000).map_err(js)?;
    Ok(())
}

fn mime_for(name: &str) -> &'static str {
    match name.rsplit('.').next().map(str::to_ascii_lowercase).as_deref() {
        Some("png") => "image/png",
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("tif" | "tiff") => "image/tiff",
        Some("webp") => "image/webp",
        Some("gif") => "image/gif",
        Some("psd" | "psb") => "image/vnd.adobe.photoshop",
        _ => "application/octet-stream",
    }
}

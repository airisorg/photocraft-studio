//! Workspace presentation only; starter files are made by PhotoCraft's existing engine.
use egui::{Align2, Color32, FontId, Pos2, Rect, RichText, Stroke, TextureHandle, Vec2, Widget as _};
use photocraft_ui_egui::theme::{self, ThemeKind, Tokens};
use std::collections::HashMap;

pub const INK: Color32 = Color32::from_rgb(35, 32, 45);
pub const MUTED: Color32 = Color32::from_rgb(115, 110, 124);
pub const PURPLE: Color32 = Color32::from_rgb(113, 72, 224);
pub const PAPER: Color32 = Color32::from_rgb(250, 249, 247);
pub const BORDER: Color32 = Color32::from_rgb(233, 230, 235);
const SELECTED: Color32 = Color32::from_rgb(242, 236, 253);
const SELECTED_ACTIVE: Color32 = Color32::from_rgb(237, 229, 252);
pub const CONTROL_HEIGHT: f32 = 40.;
pub const RELATED_GAP: f32 = 8.;
pub const ROW_GAP: f32 = 16.;
pub const SECTION_GAP: f32 = 24.;
pub const OUTER_GUTTER: i8 = 32;
pub const NARROW_GUTTER: i8 = 16;

/// Set the total gap after a vertical item, including egui's automatic item spacing.
pub fn vertical_gap(ui: &mut egui::Ui, gap: f32) {
    ui.add_space((gap - ui.spacing().item_spacing.y).max(0.));
}

pub struct Starter {
    pub slug: &'static str,
    pub name: &'static str,
    pub category: &'static str,
    pub dimensions: &'static str,
    pub background: Color32,
}
pub const STARTERS: [Starter; 6] = [
    Starter { slug: "noise", name: "Make some noise", category: "Posters", dimensions: "1080 × 1350", background: Color32::from_rgb(229, 237, 96) },
    Starter { slug: "sunday", name: "Sunday journal", category: "Posters", dimensions: "1080 × 1350", background: Color32::from_rgb(244, 237, 227) },
    Starter { slug: "next", name: "What comes next", category: "Presentations", dimensions: "1920 × 1080", background: Color32::from_rgb(35, 33, 55) },
    Starter { slug: "soul", name: "Less scroll, more soul", category: "Social", dimensions: "1080 × 1080", background: Color32::from_rgb(223, 238, 234) },
    Starter { slug: "softform", name: "Soft form", category: "Branding", dimensions: "1600 × 1000", background: Color32::from_rgb(238, 233, 245) },
    Starter { slug: "afterhours", name: "After hours", category: "Posters", dimensions: "1080 × 1350", background: Color32::from_rgb(36, 42, 38) },
];

#[derive(Clone, Copy)]
pub enum Action {
    New(u32, u32),
    Open,
    Custom,
    Template(usize),
}

pub fn primary(label: &str) -> impl egui::Widget + '_ {
    move |ui: &mut egui::Ui| action_button(ui, label, true)
}

pub fn secondary(label: &str) -> impl egui::Widget + '_ {
    move |ui: &mut egui::Ui| action_button(ui, label, false)
}

fn reduced_motion(ctx: &egui::Context) -> bool {
    let id = egui::Id::new("workspace-reduced-motion");
    let frame = ctx.cumulative_frame_nr();
    if let Some((cached_frame, reduced)) = ctx.data(|d| d.get_temp::<(u64, bool)>(id))
        && cached_frame == frame
    {
        return reduced;
    }
    #[cfg(target_arch = "wasm32")]
    let reduced = {
        // The query object stays live when the OS/browser setting changes; read it once per frame.
        thread_local! {
            static QUERY: Option<web_sys::MediaQueryList> = web_sys::window()
                .and_then(|w| w.match_media("(prefers-reduced-motion: reduce)").ok().flatten());
        }
        QUERY.with(|query| query.as_ref().is_some_and(web_sys::MediaQueryList::matches))
    };
    #[cfg(not(target_arch = "wasm32"))]
    let reduced = false;
    ctx.data_mut(|d| d.insert_temp(id, (frame, reduced)));
    reduced
}

fn hover_amount(ctx: &egui::Context, id: egui::Id, hovered: bool) -> f32 {
    if reduced_motion(ctx) {
        return if hovered { 1. } else { 0. };
    }
    ctx.animate_bool_with_time(id.with("workspace-hover"), hovered, 0.1)
}

fn action_button(ui: &mut egui::Ui, label: &str, primary: bool) -> egui::Response {
    let t = Tokens::for_kind(ThemeKind::StudioLight);
    // egui::Button reads this same response before painting; keep its semantics and layout.
    let id = ui.next_auto_id();
    let response = ui.ctx().read_response(id);
    let hovered = ui.is_enabled() && response.as_ref().is_some_and(egui::Response::hovered);
    let immediate = response.as_ref().is_some_and(|r| r.is_pointer_button_down_on() || r.has_focus());
    let base = if primary { t.primary_bg } else { t.field };
    let hover = if primary { base.lerp_to_gamma(t.primary_text, 0.08) } else { t.hover };
    let amount = hover_amount(ui.ctx(), id, hovered);
    let fill = if ui.is_enabled() && immediate {
        if primary { base.lerp_to_gamma(t.primary_text, 0.14) } else { t.pressed }
    } else {
        base.lerp_to_gamma(hover, amount)
    };
    egui::Button::new(RichText::new(label).font(theme::medium(14.)).color(if primary { t.primary_text } else { t.text }))
        .fill(fill)
        .corner_radius(t.radius_sm)
        .min_size(Vec2::new(0., CONTROL_HEIGHT))
        .ui(ui)
}

/// Keep cards readable at phone, tablet and ultrawide widths without overflowing a row.
pub fn grid_columns(width: f32, minimum: f32, maximum: usize) -> usize {
    (((width + 16.) / (minimum + 16.)).floor() as usize).clamp(1, maximum.max(1))
}

pub fn empty_message(filter: &str, searching: bool, signed_in: bool) -> (&'static str, &'static str) {
    if searching {
        return ("No matching projects", "Try another name or folder, or clear your search.");
    }
    if !signed_in {
        return ("Your workspace starts here", "Start a design now. Sign in from your avatar to save and share projects.");
    }
    match filter {
        "Trash" => ("Trash is empty", "Projects you move to Trash will appear here. You can restore them at any time."),
        "Starred" => ("Keep your favorites close", "Open a project's menu and choose Star project to find it here."),
        "Shared with me" => ("Create together", "Projects shared with your sign-in email will appear here."),
        _ => ("Make your first project", "Create a design or open a file, then choose Save design in the editor."),
    }
}

pub fn project_matches(project: &serde_json::Value, filter: &str, search: &str) -> bool {
    let trashed = project.get("trashed").and_then(serde_json::Value::as_bool) == Some(true);
    let visible = match filter {
        "Trash" => trashed,
        "Starred" => !trashed && project.get("starred").and_then(serde_json::Value::as_bool) == Some(true),
        "Shared with me" => !trashed && project.get("role").and_then(serde_json::Value::as_str).is_some_and(|r| r == "view" || r == "edit"),
        _ => !trashed,
    };
    let text = |key| project.get(key).and_then(serde_json::Value::as_str).unwrap_or("");
    visible && format!("{} {}", text("title"), text("folder")).to_lowercase().contains(&search.trim().to_lowercase())
}

/// One scale for the workspace's buttons, fields and navigation; editor density stays native.
pub fn workspace_style(ui: &mut egui::Ui) {
    let t = Tokens::for_kind(ThemeKind::StudioLight);
    *ui.visuals_mut() = egui::Visuals::light();
    let style = ui.style_mut();
    style.spacing.item_spacing = Vec2::splat(RELATED_GAP);
    style.spacing.button_padding = Vec2::new(16., 10.);
    style.spacing.interact_size.y = CONTROL_HEIGHT;
    style.text_styles.insert(egui::TextStyle::Body, FontId::proportional(14.));
    style.text_styles.insert(egui::TextStyle::Button, theme::medium(14.));
    style.visuals.override_text_color = Some(INK);
    style.visuals.selection.bg_fill = Color32::from_rgb(237, 229, 252);
    style.visuals.selection.stroke = Stroke::new(1., PURPLE);
    for (widget, fill) in [
        (&mut style.visuals.widgets.noninteractive, t.field),
        (&mut style.visuals.widgets.inactive, t.field),
        (&mut style.visuals.widgets.hovered, t.hover),
        (&mut style.visuals.widgets.active, t.pressed),
        (&mut style.visuals.widgets.open, t.hover),
    ] {
        widget.corner_radius = (t.radius_sm as u8).into();
        widget.bg_stroke = Stroke::new(1., t.field_border);
        widget.bg_fill = fill;
        widget.weak_bg_fill = fill;
        widget.expansion = 0.;
    }
    style.visuals.widgets.active.bg_stroke = Stroke::new(1., t.accent_border);
}

pub fn compact_nav(ui: &mut egui::Ui, label: &str, selected: bool) -> egui::Response {
    nav_chip(ui, label, selected, Vec2::new(0., 42.), 14.)
}

fn nav_chip(ui: &mut egui::Ui, label: &str, selected: bool, min_size: Vec2, font_size: f32) -> egui::Response {
    let t = Tokens::for_kind(ThemeKind::StudioLight);
    let id = ui.next_auto_id();
    let response = ui.ctx().read_response(id);
    let hovered = response.as_ref().is_some_and(egui::Response::hovered);
    let focused = response.as_ref().is_some_and(egui::Response::has_focus);
    let pressed = response.as_ref().is_some_and(egui::Response::is_pointer_button_down_on);
    let base = if selected { SELECTED } else { Color32::TRANSPARENT };
    let hover = if selected { SELECTED_ACTIVE } else { t.hover };
    let active = if selected { SELECTED_ACTIVE } else { t.pressed };
    let fill = if pressed || focused { active } else { base.lerp_to_gamma(hover, hover_amount(ui.ctx(), id, hovered)) };
    ui.add(
        egui::Button::new(RichText::new(label).font(theme::medium(font_size)).color(if selected { PURPLE } else { MUTED }))
            .min_size(min_size)
            .fill(fill)
            .stroke(if focused { Stroke::new(2., PURPLE) } else { Stroke::NONE })
            .corner_radius(t.radius),
    )
}

pub fn avatar(ui: &mut egui::Ui, name: Option<&str>) -> egui::Response {
    let (rect, response) = ui.allocate_exact_size(Vec2::splat(40.), egui::Sense::click());
    let center = rect.center();
    ui.painter().circle_filled(center, 20., if name.is_some() { PURPLE } else { Color32::from_rgb(239, 234, 249) });
    if let Some(name) = name {
        let initials: String = name.split_whitespace().take(2).filter_map(|s| s.chars().next()).flat_map(char::to_uppercase).collect();
        ui.painter().text(center, Align2::CENTER_CENTER, initials, FontId::proportional(14.), Color32::WHITE);
    } else {
        let stroke = Stroke::new(1.7, PURPLE);
        ui.painter().circle_stroke(center - Vec2::new(0., 5.), 4., stroke);
        ui.painter().add(egui::Shape::line(
            vec![
                center + Vec2::new(-8., 9.),
                center + Vec2::new(-8., 5.),
                center + Vec2::new(-4., 2.),
                center + Vec2::new(4., 2.),
                center + Vec2::new(8., 5.),
                center + Vec2::new(8., 9.),
            ],
            stroke,
        ));
    }
    if response.hovered() || response.has_focus() {
        ui.painter().circle_stroke(center, 22., Stroke::new(2., PURPLE));
    }
    let label = if name.is_some() { "Your account" } else { "Sign in to PhotoCraft" };
    response.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::Button, true, label));
    response.on_hover_text(label).on_hover_cursor(egui::CursorIcon::PointingHand)
}

pub fn nav_button(ui: &mut egui::Ui, label: &str, selected: bool, index: usize) -> egui::Response {
    let t = Tokens::for_kind(ThemeKind::StudioLight);
    let (rect, response) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 42.), egui::Sense::click());
    let color = if selected { PURPLE } else { MUTED };
    let base = if selected { SELECTED } else { Color32::TRANSPARENT };
    let hover = if selected { SELECTED_ACTIVE } else { PAPER };
    let fill = if response.is_pointer_button_down_on() || response.has_focus() {
        if selected { SELECTED_ACTIVE } else { t.pressed }
    } else {
        base.lerp_to_gamma(hover, hover_amount(ui.ctx(), response.id, response.hovered()))
    };
    ui.painter().rect_filled(rect, t.radius, fill);
    if response.has_focus() {
        ui.painter().rect_stroke(rect, t.radius, Stroke::new(2., PURPLE), egui::StrokeKind::Inside);
    }
    let origin = rect.min + Vec2::new(16., 13.);
    let stroke = Stroke::new(1.4, color);
    let line = |points: &[[f32; 2]]| {
        ui.painter().add(egui::Shape::line(points.iter().map(|p| origin + Vec2::new(p[0], p[1])).collect(), stroke));
    };
    match index {
        0 => {
            line(&[[0., 7.], [8., 0.], [16., 7.]]);
            line(&[[3., 6.], [3., 16.], [13., 16.], [13., 6.]]);
        }
        1 => {
            for (x, y) in [(0., 0.), (10., 0.), (0., 10.), (10., 10.)] {
                ui.painter().rect_stroke(Rect::from_min_size(origin + Vec2::new(x, y), Vec2::splat(6.)), 1., stroke, egui::StrokeKind::Inside);
            }
        }
        2 => {
            line(&[[0., 3.], [6., 3.], [8., 6.], [16., 6.], [16., 16.], [0., 16.], [0., 3.]]);
        }
        3 => {
            let points = (0..=10)
                .map(|i| {
                    let a = i as f32 * std::f32::consts::PI / 5. - std::f32::consts::FRAC_PI_2;
                    let r = if i % 2 == 0 { 8. } else { 3.7 };
                    origin + Vec2::new(8. + a.cos() * r, 8. + a.sin() * r)
                })
                .collect();
            ui.painter().add(egui::Shape::line(points, stroke));
        }
        4 => {
            ui.painter().circle_stroke(origin + Vec2::new(6., 4.), 3., stroke);
            line(&[[0., 16.], [0., 13.], [3., 10.], [9., 10.], [12., 13.], [12., 16.]]);
            line(&[[13., 3.], [16., 6.], [14., 9.]]);
        }
        _ => {
            line(&[[0., 4.], [16., 4.]]);
            line(&[[4., 1.], [12., 1.]]);
            line(&[[3., 4.], [4., 16.], [12., 16.], [13., 4.]]);
        }
    }
    ui.painter().text(rect.min + Vec2::new(44., 21.), Align2::LEFT_CENTER, label, FontId::proportional(14.), if selected { PURPLE } else { INK });
    response.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::Button, true, label));
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

fn card_image(painter: &egui::Painter, texture: &TextureHandle, rect: Rect, angle: f32) {
    let shadow = rect.translate(Vec2::new(4., 8.)).expand(2.);
    painter.rect_filled(shadow, 6., Color32::from_black_alpha(12));
    let mut mesh = egui::epaint::Mesh::with_texture(texture.id());
    let rot = egui::emath::Rot2::from_angle(angle);
    for (p, uv) in [
        (rect.left_top(), Pos2::new(0., 0.)),
        (rect.right_top(), Pos2::new(1., 0.)),
        (rect.right_bottom(), Pos2::new(1., 1.)),
        (rect.left_bottom(), Pos2::new(0., 1.)),
    ] {
        mesh.vertices.push(egui::epaint::Vertex { pos: rect.center() + rot * (p - rect.center()), uv, color: Color32::WHITE });
    }
    mesh.indices.extend([0, 1, 2, 0, 2, 3]);
    painter.add(egui::Shape::mesh(mesh));
}

pub fn hero(ui: &mut egui::Ui, textures: &HashMap<String, TextureHandle>) {
    let t = Tokens::for_kind(ThemeKind::StudioLight);
    let compact = ui.available_width() < 660.;
    let height = if compact { 206. } else { 190. };
    let (rect, _) = ui.allocate_exact_size(Vec2::new(ui.available_width(), height), egui::Sense::hover());
    ui.painter().rect_filled(rect, t.radius_lg, Color32::from_rgb(242, 236, 253));
    let text_width = if compact { rect.width() - 48. } else { rect.width() * 0.55 };
    let text_rect = Rect::from_min_size(rect.min + Vec2::new(28., 24.), Vec2::new(text_width, height - 40.));
    ui.scope_builder(egui::UiBuilder::new().max_rect(text_rect), |ui| {
        ui.label(RichText::new("A LITTLE SPACE. A LOT OF POSSIBILITY.").size(10.).strong().color(PURPLE));
        ui.add_space(13.);
        ui.label(RichText::new("Good ideas deserve\na great canvas.").size(if compact { 28. } else { 32. }).strong().color(INK));
        ui.add_space(10.);
        ui.label(RichText::new("From the first layer to the final detail. Make it yours.").size(13.).color(MUTED));
    });
    if !compact {
        let center = Pos2::new(rect.left() + rect.width() * 0.79, rect.center().y);
        ui.painter().circle_filled(center, 78., Color32::from_rgb(226, 217, 248));
        if let Some(t) = textures.get("starter/sunday") {
            card_image(ui.painter(), t, Rect::from_center_size(center + Vec2::new(-54., -4.), Vec2::new(91., 114.)), -0.17);
        }
        if let Some(t) = textures.get("starter/noise") {
            card_image(ui.painter(), t, Rect::from_center_size(center + Vec2::new(22., -4.), Vec2::new(102., 128.)), 0.12);
        }
        let tag = Rect::from_center_size(center + Vec2::new(18., 70.), Vec2::new(154., 29.));
        ui.painter().rect_filled(tag, 14., Color32::WHITE);
        ui.painter().text(tag.center(), Align2::CENTER_CENTER, "YOUR NEXT GREAT IDEA", FontId::proportional(9.), PURPLE);
    }
    ui.advance_cursor_after_rect(rect);
}

pub fn quick_sizes(ui: &mut egui::Ui) -> Option<Action> {
    let t = Tokens::for_kind(ThemeKind::StudioLight);
    let mut action = None;
    let presets = [
        ("▧", "Social post", Action::New(1080, 1080)),
        ("▯", "Story", Action::New(1080, 1920)),
        ("▱", "Slides", Action::New(1920, 1080)),
        ("▣", "Photo edit", Action::Open),
        ("+", "Blank canvas", Action::Custom),
    ];
    let columns = grid_columns(ui.available_width(), 140., 5);
    for row in presets.chunks(columns) {
        ui.columns(columns, |uis| {
            for (i, (icon, label, preset)) in row.iter().enumerate() {
                let Some(ui) = uis.get_mut(i) else {
                    continue;
                };
                let (rect, response) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 49.), egui::Sense::click());
                let fill = if response.is_pointer_button_down_on() || response.has_focus() {
                    t.pressed
                } else {
                    Color32::WHITE.lerp_to_gamma(t.hover, hover_amount(ui.ctx(), response.id, response.hovered()))
                };
                ui.painter().rect_filled(rect, t.radius_lg, fill);
                let outlined = response.hovered() || response.has_focus() || response.is_pointer_button_down_on();
                ui.painter().rect_stroke(
                    rect,
                    t.radius_lg,
                    Stroke::new(if response.has_focus() { 2. } else { 1. }, if outlined { PURPLE } else { BORDER }),
                    egui::StrokeKind::Inside,
                );
                let center = Pos2::new(rect.left() + 17., rect.center().y);
                if *icon == "+" {
                    ui.painter().line_segment([center - Vec2::new(5., 0.), center + Vec2::new(5., 0.)], Stroke::new(1.3, MUTED));
                    ui.painter().line_segment([center - Vec2::new(0., 5.), center + Vec2::new(0., 5.)], Stroke::new(1.3, MUTED));
                } else {
                    let aspect = if let Action::New(w, h) = preset { *w as f32 / *h as f32 } else { 1.5 };
                    let size = if aspect > 1. { Vec2::new(13., 13. / aspect) } else { Vec2::new(13. * aspect, 13.) };
                    ui.painter().rect_stroke(Rect::from_center_size(center, size), 1., Stroke::new(1.3, MUTED), egui::StrokeKind::Inside);
                }
                ui.painter().text(rect.center() + Vec2::new(8., 0.), Align2::CENTER_CENTER, *label, theme::medium(14.), INK);
                response.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::Button, true, *label));
                if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    action = Some(*preset);
                }
            }
        });
    }
    action
}

pub fn gallery(ui: &mut egui::Ui, textures: &HashMap<String, TextureHandle>, category: &mut String, search: &str) -> Option<Action> {
    let t = Tokens::for_kind(ThemeKind::StudioLight);
    let mut action = None;
    ui.scope(|ui| {
        // A text heading does not need the controls' 40px minimum row height.
        ui.spacing_mut().interact_size.y = 0.;
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new("Skip the blank canvas.").size(20.).strong().color(INK));
            ui.label(RichText::new("Start with something good.").size(13.).color(MUTED));
        });
    });
    vertical_gap(ui, ROW_GAP);
    ui.horizontal_wrapped(|ui| {
        for name in ["For you", "Social", "Presentations", "Posters", "Branding"] {
            let selected = category == name;
            if nav_chip(ui, name, selected, Vec2::new(75., CONTROL_HEIGHT), 12.).clicked() {
                *category = name.into();
            }
        }
    });
    vertical_gap(ui, SECTION_GAP);
    let items = STARTERS
        .iter()
        .enumerate()
        .filter(|(_, s)| {
            (category == "For you" || *category == s.category) && format!("{} {}", s.name, s.category).to_lowercase().contains(&search.to_lowercase())
        })
        .collect::<Vec<_>>();
    let columns = grid_columns(ui.available_width(), 160., 6);
    for (row_index, row) in items.chunks(columns).enumerate() {
        if row_index > 0 {
            vertical_gap(ui, SECTION_GAP);
        }
        ui.columns(columns, |uis| {
            for (i, (index, starter)) in row.iter().enumerate() {
                let Some(ui) = uis.get_mut(i) else {
                    continue;
                };
                ui.spacing_mut().item_spacing.y = RELATED_GAP / 2.;
                let width = ui.available_width();
                let height = (width * 0.94).clamp(150., 225.);
                let (rect, response) = ui.allocate_exact_size(Vec2::new(width, height), egui::Sense::click());
                ui.painter().rect_filled(rect, t.radius_lg, Color32::from_rgb(239, 237, 233));
                if let Some(texture) = textures.get(&format!("starter/{}", starter.slug)) {
                    let size = texture.size_vec2();
                    let scale = ((width - 32.) / size.x).min((height - 28.) / size.y);
                    card_image(ui.painter(), texture, Rect::from_center_size(rect.center(), size * scale), 0.);
                } else {
                    ui.painter().rect_filled(rect.shrink(15.), 3., starter.background);
                }
                if response.hovered() || response.has_focus() {
                    ui.painter().rect_stroke(rect, t.radius_lg, Stroke::new(2., PURPLE), egui::StrokeKind::Inside);
                    let badge = Rect::from_center_size(rect.center_bottom() - Vec2::new(0., 20.), Vec2::new(110., 26.));
                    ui.painter().rect_filled(badge, 13., Color32::WHITE);
                    ui.painter().text(badge.center(), Align2::CENTER_CENTER, "Use this template →", FontId::proportional(11.), INK);
                }
                response.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::Button, true, format!("Use {} template", starter.name)));
                if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    action = Some(Action::Template(*index));
                }
                vertical_gap(ui, RELATED_GAP);
                if ui.add(egui::Label::new(RichText::new(starter.name).size(13.).strong().color(INK)).truncate().sense(egui::Sense::click())).clicked() {
                    action = Some(Action::Template(*index));
                }
                ui.add(egui::Label::new(RichText::new(format!("{} · {}", starter.category, starter.dimensions)).size(12.).color(MUTED)).truncate());
            }
        });
    }
    if items.is_empty() {
        ui.label(RichText::new("No designs match that search. Try another word or category.").color(MUTED));
    }
    action
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn project_grid_is_readable_at_phone_tablet_and_desktop_widths() {
        for (width, expected) in [(354., 1), (732., 2), (1170., 4), (2200., 6)] {
            let columns = grid_columns(width, 240., 6);
            assert_eq!(columns, expected);
            assert!((width - 16. * (columns - 1) as f32) / columns as f32 >= 240.);
        }
        assert_eq!(grid_columns(0., 240., 6), 1);
        assert_eq!(grid_columns(f32::NAN, 240., 6), 1);
    }

    #[test]
    fn project_filters_preserve_trash_and_role_boundaries() {
        let own = json!({"title":"Summer café", "folder":"Brand", "role":"owner", "starred":true});
        let shared = json!({"title":"Team poster", "role":"edit"});
        let trashed = json!({"title":"Old draft", "role":"view", "trashed":true,"starred":true});
        assert!(project_matches(&own, "Home", " CAFÉ "));
        assert!(project_matches(&own, "Starred", "brand"));
        assert!(!project_matches(&own, "Shared with me", ""));
        assert!(project_matches(&shared, "Shared with me", "team"));
        assert!(!project_matches(&trashed, "Shared with me", ""));
        assert!(!project_matches(&trashed, "Starred", ""));
        assert!(project_matches(&trashed, "Trash", "draft"));
        assert!(!project_matches(&json!({}), "Shared with me", ""));
    }

    #[test]
    fn empty_states_explain_the_current_view_and_search() {
        assert_eq!(empty_message("Trash", false, true).0, "Trash is empty");
        assert!(empty_message("Starred", false, true).1.contains("Star project"));
        assert!(empty_message("Shared with me", false, true).1.contains("sign-in email"));
        assert_eq!(empty_message("Trash", true, true).0, "No matching projects");
        assert!(empty_message("Home", false, false).1.contains("avatar"));
    }
}

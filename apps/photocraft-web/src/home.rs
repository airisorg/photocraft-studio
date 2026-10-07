//! Workspace presentation only; starter files are made by PhotoCraft's existing engine.
use egui::{Align2, Color32, FontId, Pos2, Rect, RichText, Stroke, TextureHandle, Vec2};
use std::collections::HashMap;

pub const INK: Color32 = Color32::from_rgb(35, 32, 45);
pub const MUTED: Color32 = Color32::from_rgb(115, 110, 124);
pub const PURPLE: Color32 = Color32::from_rgb(113, 72, 224);
pub const PAPER: Color32 = Color32::from_rgb(250, 249, 247);
pub const BORDER: Color32 = Color32::from_rgb(233, 230, 235);

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
    Template(usize),
}

pub fn primary(label: &str) -> egui::Button<'_> {
    egui::Button::new(RichText::new(label).color(Color32::WHITE).strong()).fill(PURPLE).corner_radius(10)
}

pub fn nav_button(ui: &mut egui::Ui, label: &str, selected: bool, index: usize) -> egui::Response {
    let (rect, response) = ui.allocate_exact_size(Vec2::new(178., 42.), egui::Sense::click());
    let color = if selected { PURPLE } else { MUTED };
    if selected || response.hovered() {
        ui.painter().rect_filled(rect, 10., if selected { Color32::from_rgb(242, 236, 253) } else { PAPER });
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

pub fn hero(ui: &mut egui::Ui, textures: &HashMap<String, TextureHandle>) -> Option<Action> {
    let mut action = None;
    let compact = ui.available_width() < 660.;
    let height = if compact { 270. } else { 274. };
    let (rect, _) = ui.allocate_exact_size(Vec2::new(ui.available_width(), height), egui::Sense::hover());
    ui.painter().rect_filled(rect, 20., Color32::from_rgb(242, 236, 253));
    let text_width = if compact { rect.width() - 48. } else { rect.width() * 0.55 };
    let text_rect = Rect::from_min_size(rect.min + Vec2::new(28., 24.), Vec2::new(text_width, height - 40.));
    ui.scope_builder(egui::UiBuilder::new().max_rect(text_rect), |ui| {
        ui.label(RichText::new("A LITTLE SPACE. A LOT OF POSSIBILITY.").size(10.).strong().color(PURPLE));
        ui.add_space(13.);
        ui.label(RichText::new("Good ideas deserve\na great canvas.").size(if compact { 31. } else { 40. }).strong().color(INK));
        ui.add_space(10.);
        ui.label(RichText::new("From the first layer to the final detail. Make it yours.").size(13.).color(MUTED));
        ui.add_space(20.);
        ui.horizontal(|ui| {
            if ui.add_sized([155., 39.], primary("+  Create a design")).clicked() {
                action = Some(Action::New(1200, 900));
            }
            if ui.add_sized([118., 39.], egui::Button::new(RichText::new("Open a file ↗").color(INK)).fill(Color32::WHITE).corner_radius(10)).clicked() {
                action = Some(Action::Open);
            }
        });
    });
    if !compact {
        let center = Pos2::new(rect.left() + rect.width() * 0.79, rect.center().y);
        ui.painter().circle_filled(center, 110., Color32::from_rgb(226, 217, 248));
        if let Some(t) = textures.get("starter/sunday") {
            card_image(ui.painter(), t, Rect::from_center_size(center + Vec2::new(-75., -8.), Vec2::new(130., 163.)), -0.17);
        }
        if let Some(t) = textures.get("starter/noise") {
            card_image(ui.painter(), t, Rect::from_center_size(center + Vec2::new(30., -4.), Vec2::new(146., 183.)), 0.12);
        }
        let tag = Rect::from_center_size(center + Vec2::new(18., 104.), Vec2::new(154., 29.));
        ui.painter().rect_filled(tag, 14., Color32::WHITE);
        ui.painter().text(tag.center(), Align2::CENTER_CENTER, "YOUR NEXT GREAT IDEA", FontId::proportional(9.), PURPLE);
    }
    ui.advance_cursor_after_rect(rect);
    action
}

pub fn quick_sizes(ui: &mut egui::Ui) -> Option<Action> {
    let mut action = None;
    let presets = [
        ("▧", "Social post", 1080, 1080),
        ("▯", "Story", 1080, 1920),
        ("▱", "Slides", 1920, 1080),
        ("▣", "Photo edit", 2400, 1600),
        ("+", "Blank canvas", 1200, 900),
    ];
    let columns = if ui.available_width() < 530. { 3 } else { 5 };
    for row in presets.chunks(columns) {
        ui.columns(columns, |uis| {
            for (i, (icon, label, w, h)) in row.iter().enumerate() {
                let Some(ui) = uis.get_mut(i) else {
                    continue;
                };
                let (rect, response) = ui.allocate_exact_size(Vec2::new(ui.available_width(), 49.), egui::Sense::click());
                ui.painter().rect_filled(rect, 12., Color32::WHITE);
                ui.painter().rect_stroke(rect, 12., Stroke::new(1., if response.hovered() { PURPLE } else { BORDER }), egui::StrokeKind::Inside);
                let center = Pos2::new(rect.left() + 17., rect.center().y);
                if *icon == "+" {
                    ui.painter().line_segment([center - Vec2::new(5., 0.), center + Vec2::new(5., 0.)], Stroke::new(1.3, MUTED));
                    ui.painter().line_segment([center - Vec2::new(0., 5.), center + Vec2::new(0., 5.)], Stroke::new(1.3, MUTED));
                } else {
                    let aspect = *w as f32 / *h as f32;
                    let size = if aspect > 1. { Vec2::new(13., 13. / aspect) } else { Vec2::new(13. * aspect, 13.) };
                    ui.painter().rect_stroke(Rect::from_center_size(center, size), 1., Stroke::new(1.3, MUTED), egui::StrokeKind::Inside);
                }
                ui.painter().text(rect.center() + Vec2::new(8., 0.), Align2::CENTER_CENTER, *label, FontId::proportional(12.), INK);
                response.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::Button, true, *label));
                if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    action = Some(Action::New(*w, *h));
                }
            }
        });
        ui.add_space(8.);
    }
    action
}

pub fn gallery(ui: &mut egui::Ui, textures: &HashMap<String, TextureHandle>, category: &mut String, search: &str) -> Option<Action> {
    let mut action = None;
    ui.horizontal_wrapped(|ui| {
        ui.label(RichText::new("Skip the blank canvas.").size(23.).strong().color(INK));
        ui.label(RichText::new("Start with something good.").size(13.).color(MUTED));
    });
    ui.add_space(15.);
    ui.horizontal_wrapped(|ui| {
        for name in ["For you", "Social", "Presentations", "Posters", "Branding"] {
            let selected = category == name;
            if ui
                .add(
                    egui::Button::new(RichText::new(name).size(12.).color(if selected { PURPLE } else { MUTED }))
                        .fill(if selected { Color32::from_rgb(238, 232, 252) } else { Color32::TRANSPARENT })
                        .stroke(Stroke::NONE)
                        .corner_radius(16)
                        .min_size(Vec2::new(75., 31.)),
                )
                .clicked()
            {
                *category = name.into();
            }
        }
    });
    ui.add_space(15.);
    let items = STARTERS
        .iter()
        .enumerate()
        .filter(|(_, s)| {
            (category == "For you" || *category == s.category) && format!("{} {}", s.name, s.category).to_lowercase().contains(&search.to_lowercase())
        })
        .collect::<Vec<_>>();
    let columns = if ui.available_width() < 500. {
        2
    } else if ui.available_width() < 1000. {
        3
    } else {
        6
    };
    for row in items.chunks(columns) {
        ui.columns(columns, |uis| {
            for (i, (index, starter)) in row.iter().enumerate() {
                let Some(ui) = uis.get_mut(i) else {
                    continue;
                };
                let width = ui.available_width();
                let height = (width * 0.94).clamp(150., 225.);
                let (rect, response) = ui.allocate_exact_size(Vec2::new(width, height), egui::Sense::click());
                ui.painter().rect_filled(rect, 12., Color32::from_rgb(239, 237, 233));
                if let Some(texture) = textures.get(&format!("starter/{}", starter.slug)) {
                    let size = texture.size_vec2();
                    let scale = ((width - 32.) / size.x).min((height - 28.) / size.y);
                    card_image(ui.painter(), texture, Rect::from_center_size(rect.center(), size * scale), 0.);
                } else {
                    ui.painter().rect_filled(rect.shrink(15.), 3., starter.background);
                }
                if response.hovered() {
                    ui.painter().rect_stroke(rect, 12., Stroke::new(2., PURPLE), egui::StrokeKind::Inside);
                    let badge = Rect::from_center_size(rect.center_bottom() - Vec2::new(0., 20.), Vec2::new(110., 26.));
                    ui.painter().rect_filled(badge, 13., Color32::WHITE);
                    ui.painter().text(badge.center(), Align2::CENTER_CENTER, "Use this template →", FontId::proportional(11.), INK);
                }
                if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    action = Some(Action::Template(*index));
                }
                ui.add_space(10.);
                if ui.add(egui::Button::new(RichText::new(starter.name).size(13.).strong().color(INK)).frame(false)).clicked() {
                    action = Some(Action::Template(*index));
                }
                ui.label(RichText::new(format!("{} · {}", starter.category, starter.dimensions)).size(10.).color(MUTED));
            }
        });
        ui.add_space(24.);
    }
    if items.is_empty() {
        ui.label(RichText::new("No designs match that search. Try another word or category.").color(MUTED));
    }
    action
}

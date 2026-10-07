# Workspace spacing and corner-radius decisions

Research and source audit, 2026-10-07. This contract applies to the browser workspace.
PhotoCraft's native editor keeps its original theme and density. Values below are deliberate
PhotoCraft choices informed by observed references, not a claim that Figma and Canva share
one specification.

## The reported defect

The adjacent Open file and Create a design buttons came from two different rules:
`workspace_style` set ordinary controls to a hard-coded 9 px radius, while `primary`
explicitly used the original StudioLight `radius_sm` token, 6 px. Their shared 40 px height
did not make their silhouettes consistent. The default and primary text paths also differed.

The previous geometry regression inspected only the editor header at y=5–60. The workspace
action pair lives below it. The test could pass while the user's screenshot remained wrong.

Spacing had a second cause. egui's `add_space` is **additional** to `item_spacing`, not the
total desired gap. A 10 px implicit gap plus `add_space(16)` made a 26 px gap; adding 24
produced 34. Source values that looked intentional were not the resulting screen distances.

## What the references actually establish

| Source/context | Verified observation | Implication for PhotoCraft |
|---|---|---|
| [Canva app spacing guidance](https://www.canva.dev/docs/apps/design-guidelines/spacing/) | Related content uses smaller spacing, with examples of 4 px label/control, 8 px related content, 16 px search spacing and 24 px section separation. Parent layout owns external spacing. | Give gaps a relationship and an owner. Do not accumulate default gaps and per-child margins. These are extension guidelines, not a complete homepage specification. |
| [Atlassian spacing](https://atlassian.design/foundations/spacing/) | Uses a constrained scale based on 8 px, including smaller increments. | A scale provides consistency without forcing every gap to the same size. |
| [Atlassian radius](https://atlassian.design/foundations/radius/) | Indicative tokens include 6 px interactive controls, 8 px containment and 12 px larger containers. | PhotoCraft already has a compatible 6/8/12 token family. Reuse it rather than adding arbitrary 9/10/16 values for equivalent controls. |
| [Adobe Spectrum rounding](https://spectrum.adobe.com/foundations/styles/object-styles/rounding) | Uses several radius sizes and fully rounded buttons. | There is no universal professional-app corner radius. Radius conveys a component family; identical peers should agree. |
| [Carbon buttons](https://www.carbondesignsystem.com/building-blocks/core/components/button/guidelines) | Related controls use matching sizes; primary emphasis is reserved for the main action. | Open/Create should share geometry and differ in visual emphasis. Equal width is unnecessary. |
| Figma editor Share, observed DOM on 2026-10-07 | 32 px height, 5 px radius, 12 px horizontal padding; actual label Inter 11/16 px, weight 450. | A dense editor has a different control scale from a spacious project browser. Do not globally enlarge or replace native editing widgets. |
| Canva Create a design dialog, observed DOM on 2026-10-07 at DPR 1 | Navigation rows: 40 px height, 12 px radius, 4 px vertical gaps; actual labels Canva Sans 14/22 px. Outer dialog radius 24 px. | The live product also uses different radii and gaps by family. A navigation row is not a primary action button. |
| Canva main-navigation Create a design icon, computed style observed 2026-10-07 | `background-color`, `box-shadow` and `color` transitions 100 ms linear; transform transition 70 ms, current transform none. | A concrete reference for brief feedback. We reuse the color duration, not the icon's fully round shape or transform. This does not establish other Canva controls' timing. |
| [Atlassian motion](https://atlassian.design/foundations/motion) | Short 50–150 ms interactions, purposeful properties, reduced-motion support. | Use a 100 ms color transition for hover; do not animate control position or delay activation. |
| [Canva typography](https://www.canva.dev/docs/apps/design-guidelines/typography/) and [mobile guidance](https://www.canva.dev/docs/apps/design-guidelines/mobile/) | Typography has distinct functional roles, and layout must fit the available space. | Keep the native Inter font; prioritize useful actions and content on narrow screens. |

Reference measurements are local evidence, not copied implementation. Proprietary fonts,
CSS, assets and components are not included in the application. Computed styles must be
read from the actual text element: Canva's button container inherited Arial while its
visible child label used Canva Sans. Treating the parent font as the label font is invalid.

## PhotoCraft workspace contract

| Relationship | Decision | Reason |
|---|---:|---|
| Ordinary action/control radius | Native StudioLight `radius_sm`: 6 px | One silhouette for primary, secondary, hover, pressed and disabled states. |
| Navigation/group radius | Native StudioLight `radius`: 8 px | Distinguishes selection containers while retaining the same theme family. |
| Card radius | Native StudioLight `radius_lg`: 12 px | Existing card token; avoid unrelated per-card rounding. |
| Standard workspace action height | 40 px | Matches the workspace's search/control scale. |
| Action label | Existing Inter medium, 14 px | Same size and weight within the pair; color/fill expresses emphasis. |
| Horizontal action padding | 16 px per side | Text controls width, with a shared breathing space. |
| Adjacent related actions | 8 px | Groups Open and Create without visually joining them. |
| Related vertical items | 8 px | Repeated navigation and closely related content. |
| Action row to search | 16 px total | Separates action and search functions without making them unrelated sections. |
| Search to next section / between sections | 24 px total | Communicates a stronger content boundary. |
| Workspace outer gutter | 32 px desktop; 16 px narrow | Maintains a consistent edge and usable narrow-screen content width. |
| Sidebar gutter / section label | 16 px / native medium 12 px | Keep the label readable at DPR 1 and align the navigation surface to a deliberate edge. |
| Sidebar row / icon / text | 42 px / 16 px / 14 px | Icon and label centers share the row center; the whole row is interactive. |

The gap helper accounts for egui's implicit spacing. It does not create another layout
engine. The action pair continues to use `egui::Button` and original theme/font tokens.
The primary action is Create a design; Open file has lower visual emphasis. Hover and
press feedback can change color, but must not change height, radius, padding or position.
Circular avatars remain circular. Decorative artwork can have its own intentional shape;
it is not a reason to give sibling functional buttons different radii.

## Usability before visual consistency

Matching rectangles is necessary but insufficient. The workspace must help people find,
open and create real documents. The source/screenshot review found these separate defects:

- **Photo edit** created an empty 2400 × 1600 document. It should use the original file picker.
- **Blank canvas** silently chose 1200 × 900. It should use the original New Document dialog,
  consistent with Create a design. Named size presets retain their explicit dimensions.
- Guest Home displayed templates but hid them as soon as someone searched. Its search scope
  should include the visible templates and projects, with a matching hint.
- A 206 px decorative welcome panel pushed every template preview off the narrow-screen
  first view. Omit that panel on narrow layouts and prioritize actions and actual designs.
- Home's Recent projects showed the same full collection as All projects. Limit the recent
  set to 12 and preserve the complete collection through All projects.
- Custom navigation, categories and presets need visible keyboard focus as well as hover;
  selected state alone does not explain where keyboard input will act.

These are web-adapter corrections. File opening, dimensions, document creation, rendering,
editing and template documents continue to use PhotoCraft's existing implementation.

## Interaction feedback

Hover changes color over 100 ms using egui's existing animation machinery. Press and focus
feedback is immediate. Neither changes bounds, padding, corner radius or label position.
The primary action needs a perceptible opaque color change; multiplying an almost-black
fill by 0.93 barely changes visible RGB values and is not adequate feedback by itself.
Reduced-motion preference removes the interpolation, retaining immediate state feedback.
Animation must settle and stop requesting repaint; a decorative continuous loop would
consume local resources without helping the editing task.

This follows [Spectrum's state guidance](https://spectrum.adobe.com/foundations/behavior/states)
and [purposeful motion guidance](https://spectrum.adobe.com/foundations/behavior/motion).
It does not imply that every native editor control has been redesigned or animated.

The review also checks combined states and intermediate widths. Selected purple text on
the old pressed gray measured only 4.15:1; selected hover/focus/press retain light lavender,
with at least 4.64:1 for the chosen text and background colors. This is a color-pair check,
not general accessibility certification. Preset columns derive from a 140 px minimum
instead of abruptly switching to five columns: the old breakpoint produced 110 px tiles
at a 620 px viewport, crowding 14 px labels into their icons.

Cloud failure is another interaction state, not footer decoration. A project reservation
without a committed document must say “First save incomplete,” explain recovery, expose
owner Trash/Restore, and offer Retry save when the original document is still open. It must
not claim dimensions or a saved version, or offer a working Open/Duplicate action. Sharing
requires a completed first version. See the separate cold-worker incident in the
[verification retrospective](web-verification-retrospective.md).

The native editor's compact button implementations are 28/30 px, depend on context theme,
and also support the Pro theme's pill shape. They must not be globally changed to repair
the workspace. Use their existing font and radius tokens with the existing egui control.

## Accessibility and optical review

[WCAG 2.2 AA target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)
requires 24 × 24 CSS px or one of its defined exceptions. It is a minimum, not a recommended
comfort size. The [44 × 44 enhanced criterion](https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced.html)
is AAA. The selected 40 px desktop controls exceed the AA minimum; they do not establish
enhanced touch acceptance. A separate touch-density review remains necessary.

Keyboard focus must remain visible. Focus rings must follow the button contour and remain
unclipped; their larger outer radius is intentional. [Focus visible](https://www.w3.org/WAI/WCAG22/Understanding/focus-visible.html)
is AA; the quantified [focus appearance](https://www.w3.org/WAI/WCAG22/Understanding/focus-appearance.html)
criterion is AAA. Do not report general WCAG compliance from these geometry checks.

Check glyph bounds as well as control bounds: text can be mathematically centered while
looking off-center because of font metrics. Different labels need not have identical ink
width. Rasterized bounds can differ by roughly one pixel from layout bounds due to borders
and antialiasing. Tests should tolerate that raster effect without tolerating a 3 px radius
disagreement or an accidental extra 10 px section gap.

## Acceptance

1. Retain the reported screenshot and old-build screenshot as regression evidence.
2. Locate the actual workspace pair; do not reuse an editor-header crop.
3. Measure height, center, horizontal separation, label inset and normalized corner profiles.
   Normalize foreground/background colors so primary/secondary fill differences cannot hide
   different silhouettes. Check all four corners, not a single top-left sample.
4. Measure action-to-search and search-to-next-section distances in screenshots.
5. Exercise normal, hovered and pressed states. Confirm focus remains visible and stable.
6. Review desktop, tablet, narrow phone and high-density screenshots. Test the real Open
   file chooser and Create a design dialog after repositioning, not only their appearance.
7. Run the existing browser suite; do not weaken a functional assertion to accommodate
   changed hard-coded test coordinates. Derive clicks from the rendered control when possible.
8. Publish the tested artifact and repeat the focused workspace check on the hosted app.
9. Verify Photo edit opens the chooser, Blank canvas opens native dimensions, and guest Home
   search finds a displayed template. Verify useful design content is visible on the phone.
10. Check hover entry/exit, immediate press/focus, reduced motion and unchanged geometry.

This work closes a workspace consistency defect. It does not complete every native dialog,
phone layout, accessibility state or Figma/Canva feature. The broader acceptance ledger and
[verification retrospective](web-verification-retrospective.md) retain those boundaries.

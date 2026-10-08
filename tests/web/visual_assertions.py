"""Measure rendered controls rather than trusting their layout constants.

The editor is an egui canvas, so DOM overflow checks cannot detect mismatched buttons.
Connected regions in the actual screenshot expose their visible boundaries across themes.
"""
from collections import Counter, deque
from math import pi, sqrt
from PIL import Image, ImageChops, ImageFilter


def flat_native_canvas_bounds(image, document_rect, background=(40, 40, 40)):
    """Locate the untouched ProMedium viewport around the white fixture image."""
    rgb = image.convert('RGB')
    pixels = rgb.load()
    x0, y0, x1, y1 = document_rect
    middle = (y0+y1)//2
    def flat(x, y):
        return max(abs(a-b) for a,b in zip(pixels[x,y],background)) <= 1
    left, right = x0-10, x1+10
    if not (0 <= left < right < rgb.width and flat(left,middle) and flat(right,middle)):
        raise AssertionError('No flat native canvas beside the document')
    while left > 0 and flat(left-1,middle): left -= 1
    while right+1 < rgb.width and flat(right+1,middle): right += 1
    column = left+12
    top = bottom = middle
    while top > 0 and flat(column,top-1): top -= 1
    while bottom+1 < rgb.height and flat(column,bottom+1): bottom += 1
    bounds = (left,top,right+1,bottom+1)
    if not (left < x0 < x1 <= right and top < y0 < y1 <= bottom):
        raise AssertionError(f'Native viewport does not contain fixture: {bounds}')
    return bounds


def native_cursor_label_measurements(image, viewport, anchors, expected_count=8):
    """Measure actual ProMedium label plates; does not reconstruct placement rules."""
    rgb=image.convert('RGB');pixels=rgb.load()
    left,top,right,bottom=viewport
    fill=(83,83,83)
    remaining={(x,y) for y in range(top,bottom) for x in range(left,right)
               if max(abs(a-b) for a,b in zip(pixels[x,y],fill)) <= 1}
    rectangles=[]
    while remaining:
        seed=remaining.pop();queue=deque([seed]);points=[seed]
        while queue:
            x,y=queue.popleft()
            for point in [(x-1,y),(x+1,y),(x,y-1),(x,y+1)]:
                if point in remaining:
                    remaining.remove(point);queue.append(point);points.append(point)
        if len(points) >= 100:
            # The exact opaque interior ends one pixel inside the native stroke.
            rectangles.append((min(x for x,y in points)-1,min(y for x,y in points)-1,
                               max(x for x,y in points)+2,max(y for x,y in points)+2))
    rectangles.sort(key=lambda box:(box[1],box[0]))
    if len(rectangles)!=expected_count:
        raise AssertionError(f'Expected {expected_count} separate opaque cursor labels, got {rectangles}')
    def gap(a,b):
        return max(b[0]-a[2],a[0]-b[2],b[1]-a[3],a[1]-b[3])
    def luminance(color):
        values=[v/255 for v in color]
        return sum(weight*(v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4)
                   for v,weight in zip(values,[.2126,.7152,.0722]))
    labels=[]
    for index,box in enumerate(rectangles):
        x0,y0,x1,y1=box
        if not (left+3<=x0<x1<=right-3 and top+3<=y0<y1<=bottom-3):
            raise AssertionError(f'Cursor label is clipped/outside inset: {box}, {viewport}')
        if not (30<=x1-x0<=162 and 16<=y1-y0<=25):
            raise AssertionError(f'Cursor label is not a bounded single line: {box}')
        # One pixel allowance covers edge antialiasing; geometry promises 2px.
        if any(gap(box,other)<1 for other in rectangles[:index]):
            raise AssertionError(f'Cursor label surfaces touch/overlap: {rectangles}')
        if any(gap(box,(x-3,y-3,x+13,y+13))<1 for x,y in anchors):
            raise AssertionError(f'Cursor label covers a pointer: {box}')
        ink=[(x,y) for y in range(y0+2,y1-2) for x in range(x0+3,x1-3)
             if min(pixels[x,y])>=170 and max(pixels[x,y])-min(pixels[x,y])<=3]
        if len(ink)<12:
            raise AssertionError(f'Cursor label text is not visibly painted: {box}')
        glyph=(min(x for x,y in ink),min(y for x,y in ink),max(x for x,y in ink)+1,max(y for x,y in ink)+1)
        if glyph[3]-glyph[1]>16:
            raise AssertionError(f'Cursor label wrapped onto multiple lines: {glyph}')
        brightest=max((pixels[p] for p in ink),key=luminance)
        contrast=(luminance(brightest)+.05)/(luminance(fill)+.05)
        if contrast<4.5:
            raise AssertionError(f'Cursor label has insufficient visible contrast: {contrast}')
        # Pixel signature distinguishes the eight known fixture names without OCR.
        ink_set=set(ink)
        signature=bytes((x,y) in ink_set for y in range(glyph[1],glyph[3]) for x in range(glyph[0],glyph[2]))
        labels.append({'rect':box,'glyph_bounds':glyph,'contrast':round(contrast,3),'signature':signature})
    if len({row['signature'] for row in labels})!=expected_count:
        raise AssertionError('Fixture labels do not have eight distinct visible glyph patterns')
    for x,y in set(anchors):
        if any(max(abs(a-b) for a,b in zip(pixels[px,py],(154,107,255)))>3
               for px,py in [(x-1,y-1),(x,y-1),(x-1,y),(x,y)]):
            raise AssertionError(f'Original cursor anchor pixels moved or changed at {(x,y)}')
    return [{k:v for k,v in row.items() if k!='signature'} for row in labels]


def header_controls(image: Image.Image):
    image = image.convert('RGB')
    width = image.width
    background = image.getpixel((width-2, 5))
    pixels = image.load()
    left = max(0, width-550)
    remaining = {(x,y) for y in range(5,60) for x in range(left,width)
                 if max(abs(a-b) for a,b in zip(pixels[x,y],background)) >= 8}
    regions=[]
    while remaining:
        seed=remaining.pop()
        queue=deque([seed])
        min_x=max_x=seed[0]; min_y=max_y=seed[1]
        while queue:
            x,y=queue.popleft()
            min_x=min(min_x,x); max_x=max(max_x,x)
            min_y=min(min_y,y); max_y=max(max_y,y)
            for neighbor in [(x-1,y),(x+1,y),(x,y-1),(x,y+1)]:
                if neighbor in remaining:
                    remaining.remove(neighbor); queue.append(neighbor)
        # Ignore glyphs, separators and clipped edges; retain the button surfaces/edges.
        if min_x > left and max_x-min_x >= 29 and max_y-min_y >= 17:
            regions.append((min_x,min_y,max_x+1,max_y+1))
    return sorted(regions)


def assert_header_geometry(case, image, actions):
    rects=header_controls(image)
    case.assertEqual(len(rects),len(actions)+1, f'Missing visible header controls: {rects}')
    for name,(x0,y0,x1,y1) in zip([*actions,'Avatar'],rects):
        expected_height=40 if name=='Avatar' else 36
        case.assertAlmostEqual(y1-y0,expected_height,delta=1,
            msg=f'{name} rendered height {y1-y0}px; expected {expected_height}px; rects={rects}')
        case.assertAlmostEqual((y0+y1)/2,32,delta=1,
            msg=f'{name} is not centered in the 64px header: {rects}')
        case.assertGreaterEqual(x0,0)
        case.assertLessEqual(x1,image.width)
    for previous,current in zip(rects,rects[1:]):
        case.assertAlmostEqual(current[0]-previous[2],8,delta=2,
            msg=f'Uneven gap or overlapping controls: {rects}')
    return rects


def assert_dialog_inside(case, before, after):
    """An opened dialog must leave visible space at both sides of the viewport."""
    area=(0,64,after.width,after.height-28)
    difference=ImageChops.difference(before.convert('RGB'),after.convert('RGB')).crop(area)
    bounds=difference.convert('L').point(lambda value:255 if value>12 else 0).getbbox()
    case.assertIsNotNone(bounds,'The dialog did not open')
    case.assertGreaterEqual(bounds[0],12,f'Dialog runs off the left edge: {bounds}')
    case.assertLessEqual(bounds[2],after.width-12,f'Dialog runs off the right edge: {bounds}')
    return bounds


def _workspace_surfaces(image, top=70, bottom=440, *, background=None, left=0, right=None, minimum_size=(61, 29)):
    image = image.convert('RGB')
    pixels = image.load()
    background = background or pixels[image.width // 2, 70]
    remaining = {(x, y) for y in range(top, min(bottom, image.height))
                 for x in range(left, right or image.width)
                 if max(abs(a-b) for a, b in zip(pixels[x, y], background)) >= 8}
    surfaces = []
    while remaining:
        seed = remaining.pop()
        queue = deque([seed])
        x0 = x1 = seed[0]
        y0 = y1 = seed[1]
        while queue:
            x, y = queue.popleft()
            x0, x1 = min(x0, x), max(x1, x)
            y0, y1 = min(y0, y), max(y1, y)
            for neighbor in [(x-1, y), (x+1, y), (x, y-1), (x, y+1)]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
        if x1-x0+1 >= minimum_size[0] and y1-y0+1 >= minimum_size[1]:
            surfaces.append((x0, y0, x1+1, y1+1))
    return surfaces


def recovery_warning_action(image):
    """Find the actual native recovery action beside the amber warning text."""
    image = image.convert('RGB')
    pixels = image.load()
    amber = [(x, y) for y in range(image.height-140, image.height-28)
             for x in range(8, image.width-8)
             if pixels[x, y][0] > pixels[x, y][2]+40 and pixels[x, y][1] > pixels[x, y][2]+30]
    if not amber:
        raise AssertionError('The browser recovery warning is not visible')
    top = min(y for _, y in amber)-14
    background = image.getpixel((2, min(y for _, y in amber)))
    controls = [r for r in _workspace_surfaces(image, top, image.height-28, background=background,
                                              minimum_size=(60, 14))
                if 60 <= r[2]-r[0] <= 180 and 14 <= r[3]-r[1] <= 44]
    if len(controls) != 1:
        raise AssertionError(f'Expected one complete recovery action beside the warning: {controls}')
    return controls[0]


def session_auth_controls(image):
    """Locate the three real actions in the session warning above the editor."""
    image = image.convert('RGB')
    background = image.getpixel((2, 76))
    regions = _workspace_surfaces(image, 65, min(360, image.height-100), background=background,
                                  minimum_size=(65, 30))
    controls = sorted((r for r in regions if 65 <= r[2]-r[0] <= 240 and 34 <= r[3]-r[1] <= 42),
                      key=lambda r: (r[1], r[0]))
    if len(controls) != 3:
        raise AssertionError(f'Expected three visible session actions: {controls}')
    return controls


def workspace_project_card(image):
    """Locate the first rendered project and its visible recovery actions."""
    search = workspace_controls(image)['search']
    cards = sorted((r for r in _workspace_surfaces(image, search[3]+1, image.height-28)
                    if r[0] >= search[0]-1 and r[2]-r[0] >= 220 and r[3]-r[1] >= 220),
                   key=lambda r: (r[1], r[0]))
    if not cards:
        raise AssertionError('No visible project card')
    card = cards[0]
    background = image.convert('RGB').getpixel((card[0]+3, (card[1]+card[3])//2))
    regions = _workspace_surfaces(image, card[1]+2, card[3]-2, background=background,
                                  left=card[0]+2, right=card[2]-2)
    previews = [r for r in regions if r[2]-r[0] >= 180 and 166 <= r[3]-r[1] <= 170]
    if len(previews) != 1:
        raise AssertionError(f'Project preview is missing: {regions}')
    buttons = sorted((r for r in regions if r[1] > previews[0][3] and 28 <= r[3]-r[1] <= 42),
                     key=lambda r: (r[1], r[0]))
    return {'card': card, 'preview': previews[0], 'buttons': buttons}


def workspace_recovery_controls(image):
    """Find recovery buttons beneath an empty All projects workspace."""
    search = workspace_controls(image)['search']
    return sorted((r for r in _workspace_surfaces(image, search[3]+120, image.height-28)
                   if r[0] >= search[0] and 60 <= r[2]-r[0] <= 180 and 38 <= r[3]-r[1] <= 42),
                  key=lambda r: (r[1], r[0]))


def native_overlay_actions(before, after):
    """Locate native popup/modal buttons inside the region that actually appeared."""
    before, after = before.convert('RGB'), after.convert('RGB')
    bounds = ImageChops.difference(before, after).crop((0, 64, after.width, after.height-28)).convert('L').point(
        lambda value: 255 if value > 12 else 0).getbbox()
    if bounds is None:
        raise AssertionError('The native overlay did not appear')
    left, top, right, bottom = bounds[0], bounds[1]+64, bounds[2], bounds[3]+64
    background = Counter(after.crop((left, top, right, bottom)).getdata()).most_common(1)[0][0]
    return sorted((r for r in _workspace_surfaces(after, top, bottom, background=background, left=left, right=right)
                   if 32 <= r[3]-r[1] <= 44), key=lambda r: (r[1], r[0]))


def preferences_general_paint(image, bounds, *, stacked=False):
    """Read native General controls and complete glyph masks from actual pixels."""
    image=image.convert('RGB');pixels=image.load()
    left,top,right,bottom=bounds
    background=Counter(image.crop(bounds).getdata()).most_common(1)[0][0]
    surfaces=_workspace_surfaces(image,top+30,bottom-40,background=background,left=left+5,right=right-5,minimum_size=(10,10))
    def occupied(box):
        x0,y0,x1,y1=box
        return sum(max(abs(a-b) for a,b in zip(pixels[x,y],background))>8
                   for y in range(y0,y1) for x in range(x0,x1))/((x1-x0)*(y1-y0))
    boxes=sorted((r for r in surfaces if 12<=r[2]-r[0]<=16 and 12<=r[3]-r[1]<=16 and occupied(r)>.7),key=lambda r:r[1])
    if len(boxes)!=2 or abs(boxes[0][0]-boxes[1][0])>1:
        raise AssertionError(f'Two complete aligned General checkboxes are not painted: {boxes}')
    dropdowns=[r for r in surfaces if 20<=r[3]-r[1]<=28 and r[2]-r[0]>=180 and r[3]<boxes[0][1]]
    if not dropdowns:
        raise AssertionError('The full interpolation dropdown is clipped or missing')
    dropdown=max(dropdowns,key=lambda r:r[1])
    if any(r[0]<left+5 or r[2]>right-5 for r in [*boxes,dropdown]):
        raise AssertionError('A General control extends outside the painted dialog')
    def ink(rect):
        crop=image.crop(rect)
        mask=Image.new('L',crop.size)
        mask.putdata([255 if min(p)>=100 and max(p)-min(p)<=24 else 0 for p in crop.getdata()])
        area=mask.getbbox()
        if area is None:
            raise AssertionError(f'General preference caption/value is invisible: {rect}')
        return mask.crop(area)
    captions=[ink((r[2]+4,r[1]-3,right-6,r[3]+3)) for r in boxes]
    x0,y0,x1,y1=dropdown
    label=(x0,y0-25,x1,y0) if stacked else (max(left+5,x0-180),y0-2,x0-8,y1+2)
    masks={'interpolation_label':ink(label),'interpolation_value':ink((x0+5,y0+2,x1-22,y1-2)),
           'zoom_caption':captions[0],'home_caption':captions[1]}
    return {'checkboxes':boxes,'dropdown':dropdown,'masks':masks}


def assert_preferences_glyphs_complete(case, reference, actual):
    """Compare full native glyph silhouettes; tolerate only raster edge variation."""
    for name,expected in reference['masks'].items():
        observed=actual['masks'][name]
        case.assertAlmostEqual(observed.width,expected.width,delta=2,msg=f'{name} is clipped or changed')
        case.assertAlmostEqual(observed.height,expected.height,delta=2,msg=f'{name} wraps or is clipped')
        observed=observed.resize(expected.size,Image.Resampling.NEAREST)
        # A one-pixel dilation accommodates subpixel placement, while matching
        # both ways prevents a truncated caption or unrelated filled rectangle.
        for a,b in [(expected,observed),(observed,expected)]:
            allowed=b.filter(ImageFilter.MaxFilter(3))
            ink=sum(v>0 for v in a.getdata())
            missing=sum(v>0 for v in ImageChops.subtract(a,allowed).getdata())
            case.assertGreater(ink,25,f'{name} has too little visible text')
            case.assertLessEqual(missing/ink,.04,f'{name} glyphs differ from full native desktop text')


def _native_text_rows(image, bounds):
    """Return visible monochrome glyph bands, retaining their screen rectangles."""
    left,top,right,bottom=bounds
    crop=image.convert('RGB').crop(bounds)
    mask=Image.new('L',crop.size)
    mask.putdata([255 if min(p)>=110 and max(p)-min(p)<=24 else 0 for p in crop.getdata()])
    rows=[];start=None
    for y in range(mask.height+1):
        visible=y<mask.height and mask.crop((0,y,mask.width,y+1)).getbbox() is not None
        if visible and start is None: start=y
        if not visible and start is not None:
            band=mask.crop((0,start,mask.width,y));box=band.getbbox()
            if y-start>=6 and box[2]-box[0]>=15:
                rows.append({'rect':(left+box[0],top+start,left+box[2],top+y),'mask':band.crop(box)})
            start=None
    return rows


def preferences_section_references(image,bounds):
    """Capture complete General/Interface captions from the actual wide sidebar."""
    left,top,right,bottom=bounds
    background=Counter(image.convert('RGB').crop(bounds).getdata()).most_common(1)[0][0]
    surfaces=_workspace_surfaces(image,top+30,bottom-40,background=background,left=left+5,right=right-5,minimum_size=(1,100))
    dividers=[r for r in surfaces if r[2]-r[0]<=3 and r[3]-r[1]>=200 and left+20<r[0]<(left+right)//2]
    if len(dividers)!=1:
        raise AssertionError(f'Native wide Preferences sidebar divider is missing: {dividers}')
    divider=dividers[0]
    rows=_native_text_rows(image,(left+8,divider[1],divider[0]-6,divider[3]))
    if len(rows)<2:
        raise AssertionError('General/Interface sidebar text is incomplete')
    return dict(zip(('general','interface'),(row['mask'] for row in rows[:2])))


def compact_preferences_selector(image,bounds):
    left,top,right,bottom=bounds
    image=image.convert('RGB')
    background=Counter(image.crop(bounds).getdata()).most_common(1)[0][0]
    # Native ComboBox surfaces are24px high, below the workspace helper's29px
    # default minimum. Keep their actual closed frame and full-width contract.
    surfaces=_workspace_surfaces(image,top+30,bottom-40,background=background,left=left+5,right=right-5,minimum_size=(180,20))
    controls=sorted((r for r in surfaces if r[2]-r[0]>=max(180,right-left-32) and 20<=r[3]-r[1]<=28),key=lambda r:r[1])
    if not controls:
        raise AssertionError('Compact native section selector is not fully painted')
    x0,y0,x1,y1=controls[0]
    if not (left+5<x0<x1<right-5 and top+30<y0<y1<bottom-40):
        raise AssertionError('Compact section selector touches its capture/window boundary')
    edges=[image.getpixel(p) for p in [((x0+x1)//2,y0),((x0+x1)//2,y1-1),(x0,(y0+y1)//2),(x1-1,(y0+y1)//2)]]
    inside=image.getpixel((x0+2,(y0+y1)//2))
    if any(max(abs(a-b) for a,b in zip(edge,edges[0]))>2 for edge in edges[1:]) or max(abs(a-b) for a,b in zip(edges[0],inside))<8:
        raise AssertionError('Compact section selector lacks a complete visible native frame')
    return controls[0]


def native_preference_menu_choice(before,after,selector,reference):
    """Match a visible native popup caption, then let the test verify its semantics."""
    before,after=before.convert('RGB'),after.convert('RGB')
    area=(0,selector[3]+1,after.width,after.height)
    difference=ImageChops.difference(before,after).crop(area).convert('L').point(lambda value:255 if value>12 else 0).getbbox()
    if difference is None:
        raise AssertionError('Native section menu did not paint below its selector')
    left,top,right,bottom=difference
    bounds=(left+3,top+area[1]+3,right-3,bottom+area[1]-3)
    rows=_native_text_rows(after,bounds)
    scored=[]
    for row in rows:
        mask=row['mask']
        if not (.75<=mask.width/reference.width<=1.3 and .7<=mask.height/reference.height<=1.3):
            continue
        mask=mask.resize(reference.size,Image.Resampling.NEAREST)
        misses=[]
        for a,b in [(mask,reference),(reference,mask)]:
            count=sum(v>0 for v in a.getdata())
            misses.append(sum(v>0 for v in ImageChops.subtract(a,b.filter(ImageFilter.MaxFilter(3))).getdata())/max(1,count))
        scored.append((max(misses),row['rect']))
    scored.sort()
    # Sidebar is native medium12.5; popup is native regular13. Require a unique
    # near silhouette, never a guessed row index or a fixed source coordinate.
    if not scored or scored[0][0]>.15 or (len(scored)>1 and scored[1][0]-scored[0][0]<.02):
        raise AssertionError(f'No unique painted section-caption match: {scored}')
    return {'rect':scored[0][1],'glyph_mismatch':scored[0][0],'popup_bounds':bounds}


def sharing_invitation_controls(image):
    """Find native email, send, and member-role controls inside the sharing dialog."""
    image = image.convert('RGB')
    left, right = image.width//2-200, image.width//2+200
    top, bottom = max(80, image.height//2-320), min(image.height-80, image.height//2+320)
    background = Counter(image.getpixel((x, y))
                         for x in range(left+8, right-8, 8)
                         for y in range(image.height//2-180, image.height//2+180, 8)).most_common(1)[0][0]
    regions = _workspace_surfaces(image, top, bottom, background=background, left=left, right=right)
    fields = sorted((r for r in regions if 200 <= r[2]-r[0] <= 300 and 28 <= r[3]-r[1] <= 44),
                    key=lambda r: r[1])
    if not fields:
        raise AssertionError(f'Sharing email field missing: {regions}')
    email = fields[0]
    sends = sorted((r for r in regions if r[1] > email[3] and abs(r[0]-email[0]) <= 2
                    and 80 <= r[2]-r[0] <= 220 and 28 <= r[3]-r[1] <= 44), key=lambda r: r[1])
    if not sends:
        raise AssertionError(f'Sharing send control missing: {regions}')
    roles = sorted((r for r in regions if r[0] >= email[2] and 80 <= r[2]-r[0] <= 140
                    and 28 <= r[3]-r[1] <= 44), key=lambda r: r[1])
    return {'email': email, 'send': sends[0], 'buttons': sends, 'roles': roles, 'background': background}


def assert_invitation_feedback_geometry(case, image, controls, pending=False):
    case.assertEqual(len(controls['buttons']), 3, 'Send, Create view link and Revoke must remain fully visible')
    for rect in controls['buttons']:
        case.assertGreaterEqual(rect[3]-rect[1], 34)
        case.assertLess(rect[3], image.height-80)
    if pending:
        image = image.convert('RGB')
        x, y = controls['send'][0], controls['send'][3]
        background = controls['background']
        def ink(left, right):
            return [(px, py) for py in range(y+4, y+48) for px in range(left, right)
                    if max(abs(a-b) for a, b in zip(image.getpixel((px, py)), background)) > 40]
        spinner, label = ink(x, x+24), ink(x+46, controls['email'][2]+120)
        case.assertTrue(spinner, 'Pending spinner is not visible beside its label')
        case.assertTrue(label, 'Pending status label is not visible')
        center = (min(p[1] for p in label)+max(p[1] for p in label))/2
        # A rotating arc need not expose its whole circumference in any one frame.
        # Its visible ink must fit the compact 16px spinner around the label line.
        case.assertTrue(all(abs(py-center) <= 10 for _, py in spinner),
                        'Pending spinner falls outside the status label line')


def workspace_controls(image):
    """Locate the first pair of filled workspace actions and the search field below.

    This deliberately uses rendered surfaces, not coordinates copied from home.rs:
    narrow layouts move both actions below the title and wrap the navigation.
    """
    surfaces = [r for r in _workspace_surfaces(image) if r[3]-r[1] <= 51]
    buttons = sorted((r for r in surfaces if r[2]-r[0] < 240), key=lambda r: (r[1], r[0]))
    for left, right in zip(buttons, buttons[1:]):
        if abs(left[1]-right[1]) <= 1 and 0 < right[0]-left[2] <= 32:
            fields = [r for r in surfaces if r[1] > max(left[3], right[3])
                      and r[2]-r[0] >= 160]
            if fields:
                return {'actions': [left, right], 'search': min(fields, key=lambda r: r[1])}
    raise AssertionError(f'Workspace action pair and search field are not visible: {surfaces}')


def workspace_quick_actions(image):
    controls = workspace_controls(image)
    surfaces = _workspace_surfaces(image, controls['search'][3]+1, 760)
    actions = sorted((r for r in surfaces if 45 <= r[3]-r[1] <= 51
                      and r[0] >= controls['search'][0]), key=lambda r: (r[1], r[0]))
    if len(actions) != 5:
        raise AssertionError(f'The five quick actions are not visible: {actions}')
    return actions


def workspace_template_previews(image, has_quick_actions=True):
    search = workspace_controls(image)['search']
    top = (max(r[3] for r in workspace_quick_actions(image)) if has_quick_actions
           else search[3])+1
    previews = [r for r in _workspace_surfaces(image, top, image.height-28)
                if r[0] >= search[0]-1 and r[2]-r[0] >= 80 and r[3]-r[1] >= 64]
    return sorted(previews, key=lambda r: (r[1], r[0]))


def _workspace_button_measurement(image, rect):
    """Measure glyph bounds and four corner coverage masks independently of fill color."""
    x0, y0, x1, y1 = rect
    rgb = image.convert('RGB')
    pixels = rgb.load()
    background = pixels[x0-3, (y0+y1)//2]
    fill = Counter(pixels[x, y] for y in range(y0+8, y1-8)
                   for x in range(x0+8, x1-8)).most_common(1)[0][0]
    border = pixels[(x0+x1)//2, y0]

    def contrast(color):
        return sqrt(sum((a-b)**2 for a, b in zip(color, background)))

    # egui paints the same thin border around dark and neutral fills. Normalize to
    # both measured surfaces: fill-only normalization would discard the dark
    # button's pale border and falsely report a different radius for equal shapes.
    scale = min(contrast(fill), contrast(border))
    if scale < 5:
        raise AssertionError(f'Button has no measurable edge contrast: {rect}')
    corners = []
    for right, bottom in [(False, False), (True, False), (False, True), (True, True)]:
        corners.append([min(1., contrast(pixels[x1-1-x if right else x0+x,
                                              y1-1-y if bottom else y0+y])/scale)
                        for y in range(12) for x in range(12)])
    missing_area = sum(1-value for corner in corners for value in corner)/4
    # Area removed from a square by a quarter-circle is (1-pi/4)*r^2.
    radius = sqrt(missing_area/(1-pi/4))
    glyphs = [(x, y) for y in range(y0+8, y1-8) for x in range(x0+8, x1-8)
              if max(abs(a-b) for a, b in zip(pixels[x, y], fill)) >= 48]
    if not glyphs:
        raise AssertionError(f'Button label is not visible: {rect}')
    label = (min(x for x, _ in glyphs), min(y for _, y in glyphs),
             max(x for x, _ in glyphs)+1, max(y for _, y in glyphs)+1)
    return {'rect': rect, 'label': label, 'radius': radius, 'corners': corners, 'fill': fill,
            'horizontal_padding': [label[0]-x0, x1-label[2]]}


def assert_workspace_geometry(case, image):
    controls = workspace_controls(image)
    measurements = [_workspace_button_measurement(image, r) for r in controls['actions']]
    for name, measured in zip(['Open file', 'Create a design'], measurements):
        x0, y0, x1, y1 = measured['rect']
        label = measured['label']
        case.assertAlmostEqual(y1-y0, 40, delta=1, msg=f'{name} height: {measured["rect"]}')
        case.assertAlmostEqual((label[0]+label[2])/2, (x0+x1)/2, delta=1,
                               msg=f'{name} label is not horizontally centered: {label}')
        # Native Inter centers its line box. Descenders put the visible ink center
        # up to 2px below that center at 1x; compare the peers separately below.
        case.assertAlmostEqual((label[1]+label[3])/2, (y0+y1)/2, delta=2,
                               msg=f'{name} label ink is outside its centered native line box: {label}')
        for padding in measured['horizontal_padding']:
            case.assertAlmostEqual(padding, 16, delta=1, msg=f'{name} visible label padding')
        case.assertAlmostEqual(measured['radius'], 6, delta=1,
                               msg=f'{name} corner curve does not match the compact native radius')
    left, right = controls['actions']
    case.assertAlmostEqual(right[1], left[1], delta=1, msg='Peer action top edges differ')
    case.assertAlmostEqual(right[0]-left[2], 8, delta=1, msg='Visible peer action gap')
    case.assertAlmostEqual(controls['search'][1]-max(left[3], right[3]), 16, delta=1,
                           msg='Visible action-to-search gap includes implicit layout spacing')
    case.assertAlmostEqual(sum(measurements[0]['label'][1::2])/2,
                           sum(measurements[1]['label'][1::2])/2, delta=1,
                           msg='Peer label baselines differ')
    # Sample several columns to find the next visible content surface. This is the
    # hero on a wide screen and the first quick-action row on a narrow screen.
    rgb = image.convert('RGB')
    search = controls['search']
    columns = [search[0]+int((search[2]-search[0])*fraction) for fraction in [.25, .5, .75]]
    background = rgb.getpixel((image.width//2, 70))
    section_top = next((y for y in range(search[3]+1, min(search[3]+80, image.height))
                        if sum(max(abs(a-b) for a, b in zip(rgb.getpixel((x, y)), background)) >= 8
                               for x in columns) >= 2), None)
    case.assertIsNotNone(section_top, 'No next workspace content section below search')
    case.assertAlmostEqual(section_top-search[3], 24, delta=1, msg='Search-to-content section gap')
    # One anti-aliased edge pixel is acceptable; a three-pixel radius difference
    # removes roughly ten additional pixels from each corner and must fail.
    corner_difference = sum(abs(a-b) for a_corner, b_corner in
                            zip(measurements[0]['corners'], measurements[1]['corners'])
                            for a, b in zip(a_corner, b_corner))/4
    contour_difference = max(abs(sum(v >= .5 for v in a[i:i+12])-sum(v >= .5 for v in b[i:i+12]))
                             for a, b in zip(measurements[0]['corners'], measurements[1]['corners'])
                             for i in range(0, 144, 12))
    case.assertLessEqual(contour_difference, 1, 'Peer corner contours differ by more than one AA pixel')
    return {**controls, 'section_top': section_top,
            'buttons': [{k: v for k, v in m.items() if k != 'corners'} for m in measurements],
            'corner_difference': corner_difference, 'contour_difference': contour_difference}


def workspace_focus_changed(before, after, rect):
    """Require a visible perimeter change, separate from the button's text/fill."""
    x0, y0, x1, y1 = rect
    before, after = before.convert('RGB'), after.convert('RGB')
    border = [(x, y) for x in range(x0+12, x1-12) for y in [y0, y0+1, y1-2, y1-1]]
    border += [(x, y) for y in range(y0+12, y1-12) for x in [x0, x0+1, x1-2, x1-1]]
    return sum(max(abs(a-b) for a, b in zip(before.getpixel(p), after.getpixel(p))) >= 12
               for p in border) >= 20

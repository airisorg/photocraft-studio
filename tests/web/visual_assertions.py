"""Measure rendered header controls rather than trusting their layout constants.

The editor is an egui canvas, so DOM overflow checks cannot detect mismatched buttons.
Connected regions in the actual screenshot expose their visible boundaries across themes.
"""
from collections import deque
from PIL import Image, ImageChops


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

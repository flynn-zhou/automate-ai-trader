from PIL import Image
im = Image.open("data/board.png").convert("RGB")
w, h = im.size
px = im.load()
up = down = 0
blue = 0
rows = {}
for y in range(40, 800):
    for x in range(10, 1290):
        r, g, b = px[x, y]
        if abs(r-38) < 30 and abs(g-166) < 40 and abs(b-154) < 40:
            up += 1
        elif abs(r-239) < 30 and abs(g-83) < 40 and abs(b-80) < 40:
            down += 1
        elif abs(r-74) < 30 and abs(g-163) < 40 and abs(b-255) < 40:
            blue += 1
print("teal(up) px:", up, " red(down) px:", down, " blue px:", blue)
# how many distinct columns have candle pixels (proxy for number of drawn bars)
cols = set()
for y in range(60, 760):
    for x in range(10, 1200):
        r, g, b = px[x, y]
        if (abs(r-38) < 40 and abs(g-166) < 60) or (abs(r-239) < 40 and abs(g-83) < 60):
            cols.add(x)
print("distinct candle columns:", len(cols))

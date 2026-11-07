from PIL import Image
im = Image.open("data/board.png")
print(im.size)
im.crop((0, 40, 1300, 800)).save("data/board_chart.png")
im.crop((1290, 40, 1600, 800)).save("data/board_side.png")
print("ok")

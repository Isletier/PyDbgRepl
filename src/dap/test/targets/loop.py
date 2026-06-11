import time

x = 1
for i in range(5):
    x += i
    time.sleep(0.2)
print("done", x)

def inner(a, b):
    c = a + b
    return c


def outer():
    x = 1
    y = 2
    z = inner(x, y)
    return z


def will_raise():
    raise ValueError("boom")


result = outer()
print("result", result)

try:
    will_raise()
except ValueError:
    pass

print("done")

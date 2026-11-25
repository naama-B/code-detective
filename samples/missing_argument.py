def get_discount(price, percent):
    return price * percent / 100


def final_price(price):
    discount = get_discount(price)
    return price - discount


print(final_price(200))

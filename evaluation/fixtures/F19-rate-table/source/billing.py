from rates import RATES


def monthly(plan):
    return 30 * RATES[plan]

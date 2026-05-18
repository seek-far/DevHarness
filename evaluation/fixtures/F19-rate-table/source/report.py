from rates import RATES


def projected(plan, months):
    return months * 30 * RATES[plan]

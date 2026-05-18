def _avg(nums):
    return sum(nums) / len(nums)


def trimmed_mean(nums):
    lo = min(nums)
    hi = max(nums)
    rest = nums.copy()
    rest.remove(lo)
    rest.remove(hi)
    return _avg(nums)

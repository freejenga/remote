def apply_conditions(procedure, cycle_group=None):
    cond = procedure.conditional
    if not cond:
        return None
    low = cond.lower()
    if 'cycle 1 only' in low:
        return 'cycle 1 only'
    if 'if clinically indicated' in low or 'if abnormal' in low:
        return 'conditional'
    if 'every other cycle' in low:
        return 'every other cycle'
    return cond

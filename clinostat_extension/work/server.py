"""Non-operational fixture preserving the captured application contract shape."""

TARGET_RPM = 10.0
KP = 0.5
KI = 0.05


def control_output(error: float, integral: float) -> float:
    return KP * error + KI * integral

from extractor.validator import RateCon

CHARGES_TEXT = """1. 20% fee if not accepting the tracking sent by broker and send the prove of screenshot in the group!!!
2. LATE TO SHIPPER or RECEIVER (without permissible reason) - 10% from load rate.
3. NO TRAILER PICS FOR BOTH HOOK AND DROP FROM EACH STOP (must have pics from all sides with tires) - 10% from load rate.
4. NO BOL or POD (must have clear PDF format paperwork) - 10% from load rate"""


def format_trip(ratecon: RateCon) -> str:
    lines = []

    lines.append(f"🚛 NEW TRIP {ratecon.load_number}")
    lines.append(ratecon.carrier)

    for i, stop in enumerate(ratecon.stops, 1):
        lines.append("")
        lines.append(f"🏁 STOP {i}")
        lines.append(f"APPT: {stop.date}")
        lines.append("")

        addr = stop.address
        facility = addr.address_line_1 or ""
        street = addr.address_line_2 or ""
        city_state_zip = f"{addr.city}, {addr.state} {addr.zip}"

        if facility:
            lines.append(f"Facility: {facility}")
        lines.append(f"{street} , {city_state_zip}")
        lines.append(f"PU/DEL #: {stop.reference or 'N/A'}")
        lines.append("")
        lines.append("=======================")

    lines.append("")
    if ratecon.miles:
        lines.append(f"Miles: {ratecon.miles}")
    lines.append(f"Rate: {ratecon.total_rate_usd}")
    lines.append("")
    lines.append("===================================")
    lines.append("❗️❗️❗️CHARGES❗️❗️❗️")
    lines.append(CHARGES_TEXT)

    return "\n".join(lines)

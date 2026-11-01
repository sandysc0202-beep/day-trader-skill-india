#!/usr/bin/env python3
"""One-off helper used to port the upstream tests to the NSE clock.

Applies the same rule as retime_us_to_nse.py to the times written inside a
test file: a New York session time HH:MM (9:30 to 16:00) becomes 15 minutes
earlier in IST, a Unix timestamp in 2026 moves the same way, and 2026-10-20
(an NSE holiday) becomes 2026-10-21. Every changed test was then read by hand.
"""
import datetime
import re
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from retime_us_to_nse import move  # noqa: E402

DATE_MAP = {"2026-10-20": "2026-10-21"}


def shift_hm(h, m):
    t = h * 60 + m
    if 9 * 60 + 30 <= t <= 16 * 60:
        t -= 15
    return t // 60, t % 60


def sub_datetime(mt):
    d, h, m, rest = mt.group(1), int(mt.group(2)), int(mt.group(3)), mt.group(4) or ""
    d = DATE_MAP.get(d, d)
    if rest.startswith(":") or rest.upper().startswith("Z"):
        return mt.group(0).replace(mt.group(1), d)  # seconds / UTC: handled by hand
    h, m = shift_hm(h, m)
    return "%s%s%02d:%02d%s" % (d, mt.group(0)[10], h, m, rest)


def sub_clock(mt):
    h, m, ampm = int(mt.group(1)), int(mt.group(2)), mt.group(3)
    h24 = h
    if ampm:
        if ampm.upper().startswith("P") and h != 12:
            h24 = h + 12
        if ampm.upper().startswith("A") and h == 12:
            h24 = 0
    elif h < 9:
        return mt.group(0)
    nh, nm = shift_hm(h24, m)
    if (nh, nm) == (h24, m):
        return mt.group(0)
    if ampm:
        out_h = nh % 12 or 12
        sep = ampm[:len(ampm) - len(ampm.lstrip())]
        word = ampm.strip()
        new_word = ("AM" if nh < 12 else "PM") if word[0].isupper() else ("am" if nh < 12 else "pm")
        if "." in word:
            new_word = new_word[0] + "." + new_word[1] + "."
        return "%d:%02d%s%s" % (out_h, nm, sep, new_word)
    return ("%02d:%02d" % (nh, nm)) if len(mt.group(1)) == 2 else ("%d:%02d" % (nh, nm))


def sub_ts(mt):
    ts = int(mt.group(0))
    n = move(ts, DATE_MAP, keep_outside=True)
    return str(n) if n is not None else mt.group(0)


DT = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(:\d{2}|Z)?")
CLOCK = re.compile(r"(?<![\d:.\-])(\d{1,2}):(\d{2})(\s?[AaPp]\.?[Mm]\.?)?(?![\d:])")


def port(text):
    out, pos = [], 0
    for mt in DT.finditer(text):           # date-times first, then bare clocks in the gaps
        out.append(CLOCK.sub(sub_clock, text[pos:mt.start()]))
        out.append(sub_datetime(mt))
        pos = mt.end()
    out.append(CLOCK.sub(sub_clock, text[pos:]))
    text = "".join(out)
    text = re.sub(r"(?<!\d)17[89]\d{7}(?!\d)", sub_ts, text)
    for a, b in DATE_MAP.items():
        text = text.replace(a, b)
    return text


if __name__ == "__main__":
    for p in sys.argv[1:]:
        s = open(p, encoding="utf-8").read()
        open(p, "w", encoding="utf-8").write(port(s))

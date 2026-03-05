n = 5
scores = {"Cat1": 80, "Cat2": 60, "Cat3": 40}
eligible = [{"name": k, "score": v} for k, v in scores.items()]
slots = {}
for c in eligible: slots[c["name"]] = 1
remaining = n - len(eligible)
total_score = sum(c["score"] for c in eligible)
for c in eligible:
    share = int(remaining * (c["score"] / total_score))
    slots[c["name"]] += share
distributed = sum(slots.values())
leftover = n - distributed
for c in eligible[:leftover]:
    slots[c["name"]] += 1
print(f"n={n}, slots={slots}")

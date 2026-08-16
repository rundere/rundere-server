# Rule Sets: Trip Templates & Archetypes

**Version:** 1.0  
**Date:** August 14, 2026  
**Purpose:** Define trip archetypes and modular packing templates for deterministic selection.

---

## 1. Trip Archetypes

Each archetype is defined by **trigger conditions** (activities, duration, accommodation, transport). The system scores each archetype against the trip context and selects the top 1–2 matches.

### 1.1 Weekend City Break

| Attribute | Value |
|-----------|-------|
| **Duration** | ≤3 days |
| **Activities** | urban_walking, dining, sightseeing, shopping, nightlife |
| **Accommodation** | hotel, airbnb, boutique |
| **Transport** | flight (short-haul), train, car |
| **Bag Profile** | carry-on only, 1 bag |
| **Climate Agnostic** | Yes (modifiers apply) |

**Base Items:**
- Smart casual tops × 2–3
- Jeans/chinos × 1
- Walking shoes × 1
- Light jacket/cardigan × 1
- TSA toiletry bag
- Portable battery + chargers
- Day bag/backpack

**Modifiers:**
- `cold_weather`: Add wool sweater, coat
- `rain_gear`: Add compact umbrella, waterproof jacket
- `business_dinner`: Add blazer, dress shoes

---

### 1.2 Beach Holiday

| Attribute | Value |
|-----------|-------|
| **Duration** | 5–14 days |
| **Activities** | beach, swimming, snorkeling, sunbathing, coastal dining |
| **Accommodation** | hotel, resort, beachfront airbnb |
| **Transport** | flight (medium/long-haul) |
| **Bag Profile** | checked bag + carry-on |
| **Climate** | tropical, mediterranean, warm |

**Base Items:**
- Swimwear × 2
- Sandals/flip-flops
- Lightweight shorts × 3
- Tank tops/t-shirts × 4–5
- Sundress/linen shirt × 2
- After-sun lotion, aloe
- Waterproof pouch
- Beach towel (if not provided)
- Sun hat, sunglasses

**Modifiers:**
- `evening_dining`: Add smart casual outfit × 1
- `water_sports`: Add rash guard, water shoes
- `family_with_kids`: Add extra swimwear, kids' beach toys

---

### 1.3 Cold Weather / Ski

| Attribute | Value |
|-----------|-------|
| **Duration** | Any (typically 4–10 days) |
| **Activities** | skiing, snowboarding, winter hiking, cozy dining |
| **Accommodation** | hotel, cabin, ski lodge |
| **Transport** | flight + car, road trip |
| **Bag Profile** | checked bag (bulky items) |
| **Climate** | <10C average, snow possible |

**Base Items:**
- Thermal base layers × 2–3
- Insulated jacket/parka
- Waterproof ski pants
- Wool socks × 4–5
- Ski boots (or rent)
- Gloves, beanie, scarf
- Moisturizer, lip balm
- Hand warmers

**Modifiers:**
- `non_ski_alternative`: Add sturdy winter boots, warm layers for non-skiers
- `city_combination`: Add smart casual outfit for urban evenings
- `extreme_cold` (< -10C): Add balaclava, expedition-weight layers

---

### 1.4 Business / Conference

| Attribute | Value |
|-----------|-------|
| **Duration** | 2–7 days |
| **Activities** | business_dinner, conference, meetings, networking |
| **Accommodation** | hotel (business-class) |
| **Transport** | flight (often short/medium-haul) |
| **Bag Profile** | carry-on preferred (efficiency) |
| **Climate** | Any (climate modifiers apply) |

**Base Items:**
- Business suit/blazer × 1–2
- Dress shirts/blouses × 3–4
- Dress shoes × 1 (wear on travel day)
- Tie/scarf (optional)
- Laptop + chargers
- Business cards
- Presentation tech (clicker, adapters)
- Garment bag (if checked)

**Modifiers:**
- `casual_company_culture`: Swap suit for smart casual (blazer + chinos)
- `multi_city`: Add extra outfit for unexpected meetings
- `gala_dinner`: Add formal wear (tux/evening gown)

---

### 1.5 Backpacking / Long-Term

| Attribute | Value |
|-----------|-------|
| **Duration** | ≥14 days |
| **Activities** | hiking, hostels, mixed urban/rural, budget travel |
| **Accommodation** | hostel, camping, budget airbnb |
| **Transport** | flight + local transit (bus, train) |
| **Bag Profile** | backpack 40–60L, one-bag preferred |
| **Climate** | Variable (layering critical) |

**Base Items:**
- Quick-dry tops × 4–5
- Quick-dry bottoms × 2–3
- Underwear/socks × 7–10
- Lightweight jacket (packable)
- Hiking boots/sturdy walkers
- Sandals (for hostels/beach)
- Microfiber towel
- Padlock (for lockers)
- Laundry bag
- First aid kit + meds
- ESIM/portable WiFi
- Reusable water bottle

**Modifiers:**
- `cold_climate`: Add thermal layers, beanie
- `tropical`: Add mosquito net, quick-dry everything
- `digital_nomad`: Add laptop, portable battery, noise-canceling headphones

---

### 1.6 Road Trip

| Attribute | Value |
|-----------|-------|
| **Duration** | 3–14 days |
| **Activities** | mixed (hiking, sightseeing, beach, dining) |
| **Accommodation** | airbnb, camping, motel, hotel |
| **Transport** | car (own or rental) |
| **Bag Profile** | flexible (multiple bags OK) |
| **Climate** | Variable |

**Base Items:**
- Casual outfits × 5–7
- Comfortable shoes × 2
- Car mount/phone holder
- Cooler + reusable bottles
- Physical map backup (or offline maps)
- Snacks, water
- First aid kit
- Car charger + USB cables
- Blanket/pillow (for long drives)

**Modifiers:**
- `camping`: Add tent, sleeping bag, cooking gear
- `beach_detour`: Add swimwear, beach towel
- `hiking_focus`: Add hiking boots, daypack, poles

---

### 1.7 Cultural / Pilgrimage

| Attribute | Value |
|-----------|-------|
| **Duration** | 3–10 days |
| **Activities** | temple_visits, religious_sites, cultural_tours, modest_dining |
| **Accommodation** | hotel, ryokan, guesthouse |
| **Transport** | flight + train/local |
| **Bag Profile** | carry-on or small checked |
| **Climate** | Any (cultural norms override) |

**Base Items:**
- Modest tops (cover shoulders) × 3–4
- Long skirts/pants (cover knees) × 2–3
- Slip-on shoes (for temple removal)
- Light scarf (for head/shoulder cover)
- Day bag (for temples)
- Camera (respectful photography)
- Small cash (for offerings/donations)

**Modifiers:**
- `remove_shoes_indoors`: Emphasize slip-on shoes, avoid laces
- `head_cover_required`: Add headscarf/hat
- `business_casual_overlap`: Add blazer for formal dinners

---

## 2. Template Selection Logic

### 2.1 Scoring Algorithm

For each archetype, calculate:

```python
score = 0

# Activity match (+2 per matching activity)
for activity in trip.activities:
    if activity in archetype.activities:
        score += 2

# Accommodation match (+1)
if trip.accommodation_type in archetype.accommodation:
    score += 1

# Transport match (+1)
if trip.transport_mode in archetype.transport:
    score += 1

# Duration match (+1 if in range)
if archetype.duration_min <= trip.duration_days <= archetype.duration_max:
    score += 1

# Special event boost (+2 if applicable)
if trip.special_events intersects archetype.special_events:
    score += 2
```

**Selection:** Pick top 1–2 archetypes (tie-break by duration proximity or user preference).

### 2.2 Multi-Archetype Trips

If trip has **mixed activities** (e.g., 3 days business + 4 days beach), select **two templates** and merge:

- **Primary template:** Highest score (e.g., Business)
- **Secondary template:** Second-highest (e.g., Beach)
- **Merge logic:** Combine base items, deduplicate, apply all modifiers

---

## 3. Template Modifiers (Cross-Archetype)

Modifiers are **additive** and apply across all templates:

| Modifier | Trigger | Items Added |
|----------|---------|-------------|
| `cold_weather` | avg_temp < 10C | thermal_layers, jacket, wool_socks, beanie |
| `layering` | avg_temp 10–20C | cardigan, light_jacket, scarf |
| `lightweight` | avg_temp > 20C | breathable_tops, shorts, sandals |
| `rain_gear` | precipitation > 30% | compact_umbrella, waterproof_jacket |
| `humid` | humidity > 70% | quick_dry_clothing, anti_chafe_cream |
| `business_dinner` | activity includes business_dinner | blazer, dress_shoes, tie |
| `hiking` | activity includes day_hike | hiking_boots, daypack, water_bottle |
| `beach` | activity includes beach/swimming | swimwear_x2, sandals, waterproof_pouch |
| `temple` | venue_type = temple | modest_scarf, slip_on_shoes, cover_shoulders_knees |

---

## 4. Quantity Formulas (By Template)

### 4.1 General Rules

```python
# Base formulas (adjust by template)
tops = min(duration_days, 5) + 1
bottoms = min(ceil(duration_days / 3), 4)
underwear_socks = duration_days + 2
shoes = 2  # wear bulkiest on travel day

# If laundry_access = true:
tops = min(duration_days, 4) + 1
bottoms = min(ceil(duration_days / 4), 3)

# If one_bag_only = true:
shoes = 2  # strict limit
tops = min(tops, 5)  # cap for space
```

### 4.2 Template-Specific Adjustments

| Template | Tops | Bottoms | Shoes | Special |
|----------|------|---------|-------|---------|
| Weekend City Break | min(duration, 3) + 1 | 1–2 | 1–2 | Add blazer if business |
| Beach Holiday | duration + 1 | 2–3 | 2 (sandals + walking) | Swimwear × 2 |
| Cold Weather | 4–5 (layered) | 2–3 | 2 (boots + indoor) | Thermal × 2–3 |
| Business | 3–4 (dress shirts) | 2 (suit pants/skirt) | 1 (dress, wear on travel) | Suit × 1–2 |
| Backpacking | 4–5 (quick-dry) | 2–3 (quick-dry) | 2 (hiking + sandals) | Underwear × 7–10 |
| Road Trip | 5–7 (casual) | 3–4 | 2–3 | Comfort prioritized |
| Cultural | 3–4 (modest) | 2–3 (long) | 1–2 (slip-on) | Scarf × 1 |

---

## 5. Example: Template Selection for Kyoto Trip

**Trip Context:**
- Destination: Kyoto, Japan (JP)
- Duration: 7 days
- Activities: temple_visits, urban_walking, day_hike, business_dinner
- Accommodation: hotel
- Transport: flight + train

**Scoring:**

| Archetype | Activity Match | Accommodation | Transport | Duration | Total |
|-----------|----------------|---------------|-----------|----------|-------|
| Cultural / Pilgrimage | 2×² = 4 | 0 | 0 | +1 | **5** |
| Weekend City Break | 1×² = 2 | +1 | 0 | 0 (duration too long) | 3 |
| Business / Conference | 1×² = 2 | +1 | 0 | +1 | **4** |
| Backpacking | 0 | 0 | 0 | 0 | 0 |

**Selection:** Primary = **Cultural / Pilgrimage**, Secondary = **Business / Conference**

**Merge:** Combine cultural modesty items + business formal wear, apply climate modifiers (lightweight, humid).

---

## 6. Maintenance Notes

- **Update frequency:** Review templates quarterly (seasonal shifts, new travel trends)
- **User feedback:** Track which items are marked "unused" per template → adjust base quantities
- **Edge cases:** Add new archetypes as needed (e.g., "Digital Nomad", "Festival", "Wellness Retreat")

---

## 7. References

- Packing template best practices [web:6][web:11][web:40][web:42][web:43]
- Traveler complaints analysis [web:17][web:23][web:27][web:28]
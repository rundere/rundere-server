# Rule Sets: Packing Suggestions & Item-Level Logic

**Version:** 1.0  
**Date:** August 14, 2026  
**Purpose:** Define item-level suggestions, quantity formulas, bag assignments, and warning logic.

---

## 1. Item Categories

All items are organized into **7 core categories**:

| Category | Description | Examples |
|----------|-------------|----------|
| **Clothing** | Tops, bottoms, layers, underwear, sleepwear | quick_dry_top, jeans, light_jacket, underwear, pajamas |
| **Footwear** | Walking, formal, activity-specific | walking_shoes, dress_shoes, hiking_boots, sandals |
| **Toiletries** | Personal care, TSA-compliant, full-size | toothbrush, shampoo_tsa, sunscreen, first_aid_kit |
| **Tech** | Chargers, adapters, devices | laptop, portable_battery, universal_adapter, headphones |
| **Documents** | Travel documents, tickets, insurance | passport, flight_tickets, travel_insurance, visa |
| **Health** | Medications, supplements, medical devices | medication_insulin, cpap_machine, contact_lenses, pain_relief |
| **Activity-Specific** | Gear for specific activities | hiking_daypack, beach_towel, blazer, swimwear |

---

## 2. Quantity Formulas (By Category)

### 2.1 Clothing

**Base Formulas:**
```python
# General (balanced packing style)
tops = min(duration_days, 5) + 1  # +1 spare
bottoms = min(ceil(duration_days / 3), 4)
underwear_socks = duration_days + 2
sleepwear = ceil(duration_days / 5)  # 1 per 5 nights
layers = 1–2  # cardigan, jacket (climate-dependent)

# If laundry_access = true:
tops = min(duration_days, 4) + 1
bottoms = min(ceil(duration_days / 4), 3)
underwear_socks = duration_days + 1

# If laundry_comfort = hand_wash:
tops = min(duration_days, 4) + 1  # quick-dry recommended
bottoms = min(ceil(duration_days / 4), 3)

# If packing_style = minimalist:
tops = min(duration_days, 3) + 1
bottoms = min(ceil(duration_days / 5), 2)

# If packing_style = prepared_for_all:
tops = min(duration_days, 6) + 2
bottoms = min(ceil(duration_days / 2), 5)
```

**Climate Adjustments:**
- `cold_weather` (<10C): Add thermal_layers × 2–3, wool_socks × 5, beanie × 1
- `layering` (10–20C): Add cardigan × 1, light_jacket × 1
- `lightweight` (>20C): Prioritize breathable fabrics (linen, cotton, quick-dry), reduce layers

**User Preference Adjustments:**
- If `climate_preference = runs_cold`: Add +1 layer
- If `climate_preference = runs_hot`: Reduce layers by 1, prioritize shorts/tank tops
- If `style_constraints` includes `modest_dress`: Prioritize long skirts, loose tops, scarves

---

### 2.2 Footwear

**Base Rules:**
```python
# General
shoes = 2  # wear bulkiest on travel day

# If one_bag_only = true:
shoes = 2  # strict limit

# If activity includes hiking:
shoes = 2  # hiking_boots + walking_shoes (wear hiking boots on travel day)

# If activity includes business_dinner:
shoes = 2  # dress_shoes + walking_shoes (wear dress shoes on travel day)

# If beach_holiday:
shoes = 2  # sandals + walking_shoes
```

**Shoe Types by Activity:**
| Activity | Shoe 1 (Wear on Travel Day) | Shoe 2 (Pack) |
|----------|-----------------------------|---------------|
| City Break | walking_shoes | dress_shoes/flats |
| Beach Holiday | sandals | walking_shoes |
| Hiking | hiking_boots | sandals/light_walkers |
| Business | dress_shoes | walking_shoes |
| Cultural/Temples | slip_on_loafers | walking_shoes |

**Warnings:**
- If user marks `dress_shoes` as "unused" ≥ 2 times → suggest skipping next time
- If `shoes > 2` AND `one_bag_only = true` → warning: "You prefer one-bag travel—consider reducing to 2 pairs"

---

### 2.3 Toiletries

**Base Rules:**
```python
# If carry_on_only = true:
toiletries = "TSA_compliant_only"  # 3-1-1 rule (100ml per container)

# If checked_bag = true:
toiletries = "full_size_allowed"  # but still optimize for space

# Essential items (always include):
- toothbrush
- toothpaste
- deodorant
- sunscreen
- first_aid_kit (bandages, pain_relief, antiseptic)

# Climate-specific:
if climate = humid:
    add: anti_chafe_cream, powder
if climate = cold:
    add: moisturizer, lip_balm
if climate = beach:
    add: after_sun_lotion, aloe
```

**User Preference Adjustments:**
- If `must_have_items` includes `contact_lenses`: Add lens_solution, lens_case, backup_glasses
- If `special_needs` includes `sensory_sensitive`: Add fragrance_free_options, soft_brush

---

### 2.4 Tech

**Base Rules:**
```python
# Essential items (always include):
- phone_charger
- portable_battery (10,000+ mAh for long trips)
- universal_adapter (for international travel)

# If activity includes business:
add: laptop, laptop_charger, presentation_clicker, business_cards

# If activity includes hiking:
add: headlamp, power_bank, offline_maps

# If duration > 7 days:
add: extra_charging_cables, multi_port_charger
```

**Bag Assignment:**
- All tech items → carry_on (never check lithium batteries)
- Warning: "Keep portable battery in carry-on (TSA rule)"

---

### 2.5 Documents

**Base Rules:**
```python
# Essential items (always include):
- passport (check expiry: >6 months from travel date)
- flight_tickets (digital + printed backup)
- travel_insurance (digital + printed backup)
- visa (if required)
- hotel_reservations (digital + printed backup)

# If activity includes business:
add: business_cards, conference_badge (if pre-registered)

# If destination requires:
add: international_driving_permit, vaccination_certificate
```

**Bag Assignment:**
- All documents → carry_on (never check)
- Warning: "Keep passport and tickets in carry-on at all times"

---

### 2.6 Health

**Base Rules:**
```python
# Essential items (always include):
- prescription_medications (full duration + 3-day buffer)
- pain_relief (ibuprofen, acetaminophen)
- antihistamines
- digestive_aids (antidiarrheal, antacid)

# If must_have_items includes:
- medication_insulin: Add insulated_medication_bag, portable_cooler
- cpap_machine: Add cpap_travel_case, extension_cord, portable_battery
- contact_lenses: Add lens_solution, lens_case, backup_glasses

# If destination = remote/rural:
add: expanded_first_aid_kit, water_purification_tablets, insect_repellent
```

**Bag Assignment:**
- All medications → carry_on (never check)
- Warning: "Keep medications in original containers with prescription labels"

---

### 2.7 Activity-Specific

**By Activity:**
| Activity | Items to Add |
|----------|--------------|
| temple_visits | modest_scarf, slip_on_shoes, cover_shoulders_knees |
| hiking | hiking_boots, daypack, water_bottle, trail_snacks, poles |
| beach | swimwear_x2, sandals, waterproof_pouch, beach_towel, after_sun |
| business_dinner | blazer, dress_shoes, tie/scarf, business_cards |
| skiing | ski_boots (or rent), thermal_layers, goggles, gloves, beanie |
| road_trip | car_mount, cooler, reusable_bottles, physical_map_backup |

---

## 3. Bag Assignment Logic

### 3.1 Carry-On vs. Checked

**Carry-On Only (Always):**
- Documents (passport, tickets, insurance)
- Tech (laptop, portable_battery, chargers)
- Health (medications, medical devices)
- Valuables (jewelry, camera)
- Toiletries (if TSA-compliant)

**Checked (If Allowed):**
- Bulky items (jackets, boots)
- Full-size toiletries
- Liquids >100ml
- Sharp objects (scissors, nail clippers >6cm)

**Logic:**
```python
if user.one_bag_only = true:
    all_items = carry_on
    enforce_TSA_limits = true
    suggest_compression_cubes = true
    suggest_wear_bulkiest_on_travel_day = true

if user.bag_preferences.primary_bag_type = backpack_40L:
    prioritize_compressible_items = true
    avoid_bulky_jackets = true
    suggest_packing_cubes = true
```

### 3.2 Weight Optimization

**Formula:**
```python
# Estimate total weight
total_weight_kg = sum(item.weight_kg for all items)

# If user.weight_tolerance_kg = 10:
if total_weight_kg > 10:
    warning: "Your bag may exceed your weight tolerance (10 kg). Consider:"
    - "Wearing bulkiest shoes on travel day"
    - "Switching to travel-size toiletries"
    - "Removing optional items (e.g., extra layers)"
```

---

## 4. Warning Logic (Complaint-Driven)

### 4.1 Overpacking Warnings

```python
# If total items > threshold
if total_items > 25 AND user.one_bag_only = true:
    warning: "You prefer one-bag travel, but this list has {total_items} items. Consider:"
    - "Reducing shoes to 2 pairs"
    - "Switching to travel-size toiletries"
    - "Removing 'just in case' items"

# If user marked similar items as "unused" in past
if item.marked_unused ≥ 2 times:
    warning: "You marked '{item}' as unused on {n} past trips. Skip this time?"
```

### 4.2 Forgotten Essentials Warnings

```python
# If critical items missing
if "medications" not in packing_list:
    warning: "You usually pack medications—don't forget them!"

if "portable_battery" not in packing_list AND duration > 3:
    warning: "Consider adding a portable battery for long days out"

if "travel_insurance" not in documents:
    warning: "Travel insurance is recommended for international trips"
```

### 4.3 Cultural/Weather Mismatch Warnings

```python
# If cultural norms violated
if destination = Japan AND item = high_heels:
    warning: "High heels are impractical for temple visits (shoes removed frequently). Consider slip-on shoes."

if destination = UAE AND item = tank_top:
    warning: "Tank tops may not be appropriate for public places in UAE. Consider loose, long-sleeve tops."

# If weather mismatch
if forecast.precipitation > 40% AND "umbrella" not in packing_list:
    warning: "Forecast shows {precip}% chance of rain—consider adding an umbrella."

if forecast.avg_temp < 10C AND "jacket" not in packing_list:
    warning: "Average temperature is {temp}C—consider adding a warm jacket."
```

### 4.4 Bag Limit Warnings

```python
# If carry-on dimensions exceeded
if carry_on_dimensions > airline_limit:
    warning: "Your carry-on may exceed airline limits ({airline_limit}). Consider:"
    - "Using a smaller bag"
    - "Checking a bag"

# If weight tolerance exceeded
if total_weight_kg > user.weight_tolerance_kg:
    warning: "Your bag may exceed your weight tolerance ({tolerance} kg). Consider removing:"
    - "Heavy items (e.g., full-size toiletries, extra shoes)"
    - "Optional items (e.g., extra layers, 'just in case' items)"
```

---

## 5. Example: Item-Level Logic for Kyoto Trip

**Trip Context:**
- Destination: Kyoto, Japan (JP)
- Duration: 7 days
- Activities: temple_visits, urban_walking, day_hike, business_dinner
- User: female, one_bag_only = true, runs_cold, modest_dress, cultural_strictness = strict

**Item-Level Logic:**

### 5.1 Clothing
```python
tops = min(7, 4) + 1 = 5  # quick-dry, modest (cover shoulders)
bottoms = min(ceil(7 / 4), 3) = 3  # long skirts/pants
underwear_socks = 7 + 2 = 9
layers = 2  # light_cardigan, scarf (for temples + runs_cold)
sleepwear = ceil(7 / 5) = 2
```

**Add:**
- quick_dry_top × 5 (modest, breathable)
- long_skirt × 2, loose_pants × 1
- light_cardigan × 1, modest_scarf × 1
- underwear × 9, socks × 9
- pajamas × 2

**Remove:**
- Tank tops (culturally inappropriate for temples)
- High heels (impractical for temple removal)

### 5.2 Footwear
```python
shoes = 2  # slip_on_loafers (for temples), walking_shoes
```

**Add:**
- slip_on_loafers × 1 (wear on travel day for easy temple removal)
- walking_shoes × 1 (pack)

**Remove:**
- High heels (user constraint: no_heels)
- Heavy boots (one_bag_only = true)

### 5.3 Toiletries
```python
toiletries = TSA_compliant_only  # one_bag_only = true
```

**Add:**
- toothbrush, toothpaste_tsa, deodorant_tsa
- sunscreen_tsa, first_aid_kit_tsa
- moisturizer (runs_cold + humid climate)

**Remove:**
- Full-size shampoo, conditioner (TSA limit)

### 5.4 Tech
```python
# Essential items
add: phone_charger, portable_battery, universal_adapter
```

**Add:**
- phone_charger, portable_battery (10,000 mAh), universal_adapter (Japan uses Type A/B)
- noise_canceling_headphones (must_have_items)

**Bag Assignment:**
- All tech → carry_on

### 5.5 Documents
```python
# Essential items
add: passport, flight_tickets, travel_insurance
```

**Add:**
- passport (check expiry), flight_tickets (digital + printed), travel_insurance (digital)

**Bag Assignment:**
- All documents → carry_on

### 5.6 Health
```python
# Essential items
add: prescription_medications, pain_relief, antihistamines
```

**Add:**
- medication_insulin (must_have_items) → insulated_medication_bag, portable_cooler
- pain_relief, antihistamines, digestive_aids

**Bag Assignment:**
- All health items → carry_on

**Warning:**
- "Keep insulin in carry-on with cooling pack—check airline rules for liquids"

### 5.7 Activity-Specific
```python
# temple_visits
add: modest_scarf, slip_on_shoes (already added)

# business_dinner
add: blazer, dress_shoes (but user no_heels → suggest flats)

# day_hike
add: daypack, water_bottle, trail_snacks
```

**Add:**
- modest_scarf × 1 (for temple coverage)
- blazer × 1 (for business dinner)
- daypack × 1, water_bottle × 1, trail_snacks

**Remove:**
- Dress shoes (user constraint: no_heels → suggest flats instead)

---

## 6. Maintenance Notes

- **Update frequency:** Review item suggestions quarterly (new products, travel trends)
- **User feedback:** Track which items are marked "unused" → adjust quantities or remove
- **Airline rule changes:** Update TSA/airline limits as needed (e.g., liquid restrictions)
- **Cultural updates:** Add new cultural norms as destinations evolve

---

## 7. References

- Packing list best practices [web:6][web:11][web:40][web:42][web:43]
- Traveler complaints analysis [web:17][web:23][web:27][web:28]
- Cultural etiquette guidelines [web:44][web:45][web:46][web:49][web:52][web:55][web:58]
# Rule Sets: User Profile & Preferences

**Version:** 1.0  
**Date:** August 14, 2026  
**Purpose:** Define user profile schema, preference logic, and personalization rules.

---

## 1. User Profile Schema

### 1.1 Core Fields

```json
{
  "user_id": "uuid",
  "gender": "male|female|nonbinary|prefer_not_to_say",
  "age_range": "18-24|25-34|35-44|45-54|55+",
  "climate_preference": "runs_cold|neutral|runs_hot",
  "style_constraints": ["no_heels", "modest_dress", "business_casual_only", "vegan_leather_only"],
  "must_have_items": ["medication_insulin", "cpap_machine", "contact_lenses", "noise_canceling_headphones"],
  "bag_preferences": {
    "primary_bag_type": "carry_on_spin|backpack_40L|checked_large|duffel",
    "max_bags": 2,
    "one_bag_only": true|false,
    "weight_tolerance_kg": 10,
    "preferred_brands": ["Away", "Patagonia", "Osprey"]
  },
  "laundry_comfort": "hand_wash|machine|dry_clean_only|no_laundry",
  "past_trip_feedback": [
    {
      "item": "dress_shoes",
      "trip_type": "city_break",
      "used": false,
      "reason": "too_heavy",
      "trip_date": "2026-06-15"
    }
  ],
  "cultural_strictness": "strict|flexible|custom",
  "packing_style": "minimalist|prepared_for_all|balanced",
  "special_needs": ["mobility_assistance", "dietary_restrictions", "sensory_sensitive"],
  "created_at": "2026-01-10T08:00:00Z",
  "updated_at": "2026-08-14T14:30:00Z"
}
```

---

## 2. Field Definitions & Logic

### 2.1 Gender

**Purpose:** Influences clothing templates (e.g., dress vs. suit options, swimwear styles).

**Values:**
- `male`: Suggest suits, dress shirts, men's swimwear
- `female`: Suggest dresses, blouses, women's swimwear, modesty options (scarves, cardigans)
- `nonbinary`: Gender-neutral templates (mix of tops/bottoms, avoid gendered language)
- `prefer_not_to_say`: Use neutral templates, ask for style preferences instead

**Logic:**
- If `gender = female` AND `style_constraints` includes `modest_dress` → prioritize long skirts, loose tops, scarves
- If `gender = male` AND `style_constraints` includes `business_casual_only` → suggest chinos, blazers, loafers

---

### 2.2 Age Range

**Purpose:** Adjusts item suggestions (e.g., comfort vs. style, mobility considerations).

**Values:**
- `18-24`: Budget-friendly, trendy, lightweight
- `25-34`: Balanced (style + practicality)
- `35-44`: Comfort-focused, quality over quantity
- `45-54`: Comfort prioritized, mobility-friendly items
- `55+`: Comfort + accessibility (easy-on shoes, lightweight bags)

**Logic:**
- If `age_range = 55+` → suggest slip-on shoes, lightweight bags, compression socks
- If `age_range = 18-24` → suggest budget-friendly options, trendy items, hostels-compatible gear

---

### 2.3 Climate Preference

**Purpose:** Adjusts layering recommendations based on personal thermal comfort.

**Values:**
- `runs_cold`: Add extra layers, warmer fabrics (wool, fleece)
- `neutral`: Standard layering
- `runs_hot`: Prioritize breathable fabrics (linen, cotton, quick-dry), reduce layers

**Logic:**
- If `climate_preference = runs_cold` AND destination avg_temp < 20C → add thermal layers, wool socks, beanie
- If `climate_preference = runs_hot` AND destination avg_temp > 25C → suggest shorts, tank tops, sandals, avoid heavy fabrics

---

### 2.4 Style Constraints

**Purpose:** Enforce personal style boundaries (e.g., no heels, modest dress, vegan materials).

**Common Values:**
- `no_heels`: Remove all heels from suggestions
- `modest_dress`: Prioritize coverage (shoulders, knees, chest)
- `business_casual_only`: Avoid overly casual items (no athletic wear, flip-flops)
- `vegan_leather_only`: Suggest synthetic materials, avoid wool/leather
- `plus_size_friendly`: Prioritize brands with extended sizing
- `petite_friendly`: Prioritize brands with petite sizing

**Logic:**
- If `style_constraints` includes `no_heels` → remove all heels, suggest flats/loafers/sneakers
- If `style_constraints` includes `modest_dress` → prioritize long skirts, loose tops, scarves, avoid revealing swimwear

---

### 2.5 Must-Have Items

**Purpose:** Force-add critical items regardless of template.

**Common Values:**
- `medication_insulin`: Add insulated medication bag, portable cooler
- `cpap_machine`: Add CPAP travel case, extension cord, portable battery
- `contact_lenses`: Add lens solution, case, backup glasses
- `noise_canceling_headphones`: Add for long flights
- `portable_battery`: Add for tech-heavy travelers
- `kindle_tablet`: Add for long trips

**Logic:**
- Always add `must_have_items` to final packing list (override template exclusions)
- If `must_have_items` includes `medication_insulin` → add warning: "Keep in carry-on, check airline rules for liquids"

---

### 2.6 Bag Preferences

**Purpose:** Enforce luggage constraints (size, weight, number of bags).

**Fields:**
- `primary_bag_type`: Preferred bag style (carry-on spin, backpack 40L, checked large, duffel)
- `max_bags`: Maximum number of bags user is willing to check/carry
- `one_bag_only`: Boolean (if true, enforce strict one-bag limits)
- `weight_tolerance_kg`: Maximum weight user is comfortable carrying (e.g., 10 kg for carry-on)
- `preferred_brands`: Optional (for personalized recommendations)

**Logic:**
- If `one_bag_only = true` → enforce strict item limits (shoes ≤ 2, tops ≤ 5, no full-size toiletries)
- If `primary_bag_type = backpack_40L` → prioritize compressible items, avoid bulky jackets
- If `weight_tolerance_kg = 7` → flag heavy items (e.g., "Hiking boots add 1.2 kg—consider wearing on travel day")

---

### 2.7 Laundry Comfort

**Purpose:** Adjust clothing quantities based on willingness/ability to do laundry.

**Values:**
- `hand_wash`: Suggest quick-dry fabrics, travel sink stopper, small detergent
- `machine`: Standard quantities (can wash mid-trip)
- `dry_clean_only`: Suggest wrinkle-resistant fabrics, limit items
- `no_laundry`: Pack full duration quantities

**Logic:**
- If `laundry_comfort = hand_wash` → reduce clothing quantities by 30%, add quick-dry items
- If `laundry_comfort = no_laundry` → pack full duration quantities (tops = duration + 2)

---

### 2.8 Past Trip Feedback

**Purpose:** Learn from user's history (downweight unused items, prioritize favorites).

**Schema:**
```json
{
  "item": "dress_shoes",
  "trip_type": "city_break",
  "used": false,
  "reason": "too_heavy|uncomfortable|never_occasion|weather_mismatch",
  "trip_date": "2026-06-15"
}
```

**Logic:**
- If item marked `used = false` ≥ 2 times for same trip type → downweight or hide from future suggestions
- If item marked `used = true` ≥ 3 times → prioritize in future lists
- If `reason = too_heavy` → suggest lighter alternative (e.g., "Consider loafers instead of dress shoes")
- If `reason = uncomfortable` → flag for review ("You marked 'hiking boots' uncomfortable—try breaking them in before next trip")

---

### 2.9 Cultural Strictness

**Purpose:** Control enforcement level of cultural norms.

**Values:**
- `strict` (default for first-time travelers): Enforce all cultural norms (e.g., must cover shoulders at temples)
- `flexible` (experienced travelers): Suggest but don't enforce (user can override)
- `custom`: User-defined overrides (e.g., "I always cover knees" or "I avoid headscarves")

**Logic:**
- If `cultural_strictness = strict` → enforce all cultural rules (block non-compliant items)
- If `cultural_strictness = flexible` → show warnings but allow overrides ("This item may not be appropriate for temples—proceed?")
- If `cultural_strictness = custom` → apply user-defined rules (stored in profile)

---

### 2.10 Packing Style

**Purpose:** Adjust item quantities and variety based on user's packing philosophy.

**Values:**
- `minimalist`: Prioritize multi-use items, limit quantities (tops = 3–4, shoes = 2)
- `prepared_for_all`: Pack for contingencies (extra layers, backup items, first aid kit)
- `balanced`: Middle ground (standard formulas)

**Logic:**
- If `packing_style = minimalist` → reduce quantities by 20%, prioritize multi-use items (e.g., scarf that doubles as blanket)
- If `packing_style = prepared_for_all` → add contingency items (extra meds, backup chargers, rain jacket even if low precip chance)

---

### 2.11 Special Needs

**Purpose:** Accommodate accessibility, dietary, or sensory requirements.

**Common Values:**
- `mobility_assistance`: Suggest lightweight bags, slip-on shoes, compression socks
- `dietary_restrictions`: Add snack options, note restaurant considerations
- `sensory_sensitive`: Prioritize soft fabrics, avoid tags/seams, noise-canceling headphones
- `medical_conditions`: Add specific meds, medical devices, doctor's notes

**Logic:**
- If `special_needs` includes `mobility_assistance` → suggest wheeled bags, avoid heavy backpacks, add compression socks
- If `special_needs` includes `sensory_sensitive` → prioritize seamless clothing, soft fabrics, noise-canceling headphones

---

## 3. Personalization Rules

### 3.1 Item Weighting (Based on Feedback)

Each item has a **user-specific weight** (0–100) based on past usage:

```python
# Initial weight
item_weight = 50

# Adjust based on feedback
if item marked "used = true":
    item_weight += 10
elif item marked "used = false":
    item_weight -= 20

# If weight < 20, hide from suggestions
if item_weight < 20:
    show_item = False
elif item_weight > 80:
    prioritize_item = True  # Show at top of list
```

### 3.2 Style Matching

If user consistently selects certain styles (e.g., "linen shirts", "black pants"), prioritize similar items:

```python
# Extract style patterns from past trips
if user selected "linen_shirt" ≥ 3 times:
    prioritize "linen_shirt" in future warm-weather trips

if user selected "black_pants" ≥ 3 times:
    prioritize "black_pants" in future city trips
```

### 3.3 Bag Optimization

If user prefers `one_bag_only`, optimize for space/weight:

```python
# One-bag optimization
if user.one_bag_only = true:
    max_shoes = 2
    max_tops = 5
    max_bottoms = 3
    toiletries = "TSA_only"
    suggest_compression_cubes = True
    suggest_wear_bulkiest_on_travel_day = True
```

---

## 4. Example: User Profile Application

**User Profile:**
- Gender: female
- Age range: 35–44
- Climate preference: runs_cold
- Style constraints: no_heels, modest_dress
- Must-have items: medication_insulin, noise_canceling_headphones
- Bag preferences: one_bag_only = true, primary_bag_type = carry_on_spin
- Laundry comfort: hand_wash
- Cultural strictness: strict
- Packing style: balanced

**Trip:** Kyoto, Japan, 7 days, mixed activities (temple, business, urban)

**Personalization Logic:**
1. **Gender + modest_dress:** Prioritize long skirts, loose tops, scarves
2. **runs_cold + Kyoto temp (20–28C):** Add light cardigan, avoid tank tops
3. **no_heels:** Remove all heels, suggest slip-on loafers, flats
4. **medication_insulin:** Add insulated medication bag, portable cooler (carry-on only)
5. **one_bag_only:** Enforce strict limits (tops = 4, bottoms = 2, shoes = 2)
6. **hand_wash:** Reduce quantities by 30%, add quick-dry items
7. **cultural_strictness = strict:** Enforce temple modesty (cover shoulders/knees), add slip-on shoes for removal
8. **packing_style = balanced:** Standard formulas (no extreme minimalism or overpacking)

**Final Packing List Adjustments:**
- Add: Light cardigan, modest scarf, slip-on loafers, insulated medication bag, quick-dry tops
- Remove: Heels, tank tops, full-size toiletries, heavy jackets
- Quantities: Tops = 4, bottoms = 2, underwear = 9 (7 + 2), shoes = 2

---

## 5. Maintenance Notes

- **Onboarding:** Guide users through profile setup (5–7 minutes)
- **Updates:** Allow users to edit profile anytime (e.g., "I now prefer one-bag travel")
- **Feedback loop:** After each trip, prompt: "Which items did you use/not use? Why?"
- **Privacy:** Store profile securely (encrypted), allow deletion on request

---

## 6. References

- User preference best practices [web:6][web:11][web:40][web:42]
- Traveler complaints analysis [web:17][web:23][web:27][web:28]
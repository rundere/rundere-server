# Packing Solution Specification

**Version:** 1.0  
**Date:** August 14, 2026  
**Status:** Draft for Implementation

---

## 1. Executive Summary

This document specifies a **deterministic, profile-driven packing solution** that generates personalized packing lists by combining:

- **Authenticated user profiles** (preferences, constraints, history)
- **Trip context** (destination, dates, activities, accommodation, transport)
- **Modular templates** (by trip archetype)
- **Rules engines** (climate, cultural, bag constraints, activity overrides)

The system is designed to **address top traveler complaints**: overpacking, forgotten essentials, inappropriate items for culture/weather, and bag mismanagement. [web:17][web:23][web:27]

**Core Principle:** No monolithic "master list." Instead, use **dynamic, context-aware templates** that adapt to user + trip constraints.

---

## 2. System Architecture Overview

```
┌─────────────────────┐
│   User Profile      │
│ (authenticated,     │
│  persistent)        │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐     ┌─────────────────────┐
│   Trip Context      │────▶│ Template Selection  │
│ (destination, dates,│     │ (deterministic      │
│  activities, etc.)  │     │  rules engine)      │
└──────────┬──────────┘     └──────────┬──────────┘
           │                           │
           ▼                           ▼
┌─────────────────────┐     ┌─────────────────────┐
│ Climate & Cultural  │     │ Bag & Duration      │
│ Overrides           │     │ Constraints         │
└──────────┬──────────┘     └──────────┬──────────┘
           │                           │
           └─────────────┬─────────────┘
                         ▼
               ┌─────────────────────┐
               │ Final Packing List  │
               │ (categorized,       │
               │  bag-assigned,      │
               │  quantity-scaled)   │
               └─────────────────────┘
```

---

## 3. Required Inputs

### 3.1 User Profile (Authenticated, Persistent)

**Schema:**
```json
{
  "user_id": "uuid",
  "gender": "male|female|nonbinary|prefer_not_to_say",
  "age_range": "18-24|25-34|35-44|45-54|55+",
  "climate_preference": "runs_cold|neutral|runs_hot",
  "style_constraints": ["no_heels", "modest_dress", "business_casual_only"],
  "must_have_items": ["medication_insulin", "cpap_machine", "contact_lenses"],
  "bag_preferences": {
    "primary_bag_type": "carry_on_spin|backpack_40L|checked_large",
    "max_bags": 2,
    "one_bag_only": true|false,
    "weight_tolerance_kg": 10
  },
  "laundry_comfort": "hand_wash|machine|dry_clean_only",
  "past_trip_feedback": [
    {"item": "dress_shoes", "trip_type": "city_break", "used": false, "reason": "too_heavy"}
  ],
  "cultural_strictness": "strict|flexible|custom"
}
```

**Notes:**
- `gender` influences clothing templates (e.g., dress vs. suit options)
- `climate_preference` adjusts layering recommendations
- `past_trip_feedback` enables learning (downweight unused items)
- `cultural_strictness` controls enforcement level of cultural norms

### 3.2 Trip Context (Per-Trip Input)

**Schema:**
```json
{
  "destination": {
    "city": "Kyoto",
    "country": "Japan",
    "country_code": "JP",
    "climate_zone": "humid_subtropical",
    "cultural_norms": ["modest_dress_temples", "remove_shoes_indoors"]
  },
  "start_date": "2026-09-15",
  "end_date": "2026-09-22",
  "duration_days": 7,
  "activities": [
    {"name": "temple_visits", "venue_type": "temple", "days": [1, 3]},
    {"name": "urban_walking", "venue_type": "city", "days": [1, 2, 4]},
    {"name": "day_hike", "venue_type": "trail", "days": [5]},
    {"name": "business_dinner", "venue_type": "restaurant_formal", "days": [6]}
  ],
  "accommodation_type": "hotel|hostel|airbnb|camping|ryokan",
  "laundry_access": true|false|"limited",
  "transport_mode": "flight|train|road_trip|cruise",
  "special_events": ["wedding", "conference", "festival"],
  "weather_forecast": {
    "avg_temp_c": 24,
    "precipitation_chance": 30,
    "humidity": 70
  }
}
```

**Notes:**
- `activities` are tagged with `venue_type` for cultural/activity overrides
- `weather_forecast` can be fetched via API (e.g., OpenWeatherMap) or use historical averages
- `special_events` trigger additional items (e.g., formal wear for weddings)

---

## 4. Deterministic Layer: Rules Engine

### 4.1 Template Selection (Archetype Scoring)

**Process:**
1. **Score each archetype** by matching trip context:
   - +2 for each activity match
   - +1 for accommodation match
   - +1 for transport match
   - +1 if duration in archetype range
2. **Select top 1–2 archetypes** (tie-break by duration or user preference)

**Archetype Definitions:** (See `rule_sets_templates.md` for full list)

| Archetype | Duration | Activities | Accommodation | Transport |
|-----------|----------|------------|---------------|-----------|
| Weekend City Break | ≤3 | urban_walking, dining, sightseeing | hotel/airbnb | flight/train |
| Beach Holiday | 5–14 | beach, swimming | hotel/resort | flight |
| Cold Weather / Ski | any | skiing, snowboarding | hotel/cabin | flight/car |
| Business / Conference | 2–7 | business_dinner, conference | hotel | flight |
| Backpacking / Long-Term | ≥14 | hiking, hostels | hostel/camping | flight + local |
| Road Trip | 3–14 | mixed | airbnb/camping/motel | car |
| Cultural / Pilgrimage | 3–10 | temple_visits, religious_sites | hotel/ryokan | flight/train |

### 4.2 Climate Modifiers

**Logic:**
- **Temperature bands:**
  - `<10C` → add `cold_weather` modifier (thermal layers, jacket)
  - `10–20C` → add `layering` modifier (cardigan, light jacket)
  - `>20C` → add `lightweight` modifier (breathable fabrics, shorts)
- **Precipitation >30%** → add `rain_gear` modifier (umbrella, waterproof jacket)
- **Humidity >70%** → flag `avoid_cotton`, suggest `quick_dry`

**Data Source:** Weather API or historical climate data by destination + dates.

### 4.3 Bag & Duration Constraints

**Bag Limits:**
- **Carry-on only:** Enforce dimensions (e.g., 55x40x20 cm), weight <10 kg, no full-size liquids
- **One-bag preference:** Prioritize multi-use items, limit shoes to 2 pairs
- **Checked bag allowed:** Relax weight/size constraints, but still optimize

**Duration Scaling Formulas:**
```python
# Clothing quantities
tops = min(duration_days, 5) + 1  # +1 spare
bottoms = min(ceil(duration_days / 3), 4)
underwear_socks = duration_days + 2
shoes = 2  # wear bulkiest on travel day

# If laundry_access = true:
tops = min(duration_days, 4) + 1
bottoms = min(ceil(duration_days / 4), 3)
```

### 4.4 Cultural Overrides

**Process:**
1. **Lookup base rules** by `destination.country_code` (see `rule_sets_cultural.md`)
2. **Merge activity-specific overrides** by `venue_type` (e.g., temple → modest dress, remove shoes)
3. **Apply seasonal/event flags** (e.g., Ramadan → modest dress in public)
4. **Respect user preference** (`cultural_strictness`: strict/flexible/custom)

**Output:** Final cultural constraints (e.g., "must cover shoulders", "remove shoes at temples", "no heels in ryokan")

### 4.5 Activity-Specific Additions

**Logic:**
- **Hiking:** Add `hiking_boots`, `daypack`, `water_bottle`, `trail_snacks`
- **Business:** Add `blazer`, `dress_shoes`, `presentation_tech`, `business_cards`
- **Beach:** Add `swimwear_x2`, `sandals`, `waterproof_pouch`, `after_sun`
- **Temple/Religious:** Add `modest_scarf`, `slip_on_shoes`, `cover_shoulders_knees`

---

## 5. Template Library Structure

Each template is a **modular JSON object** with base items + modifiers:

```json
{
  "template_id": "weekend_city_break",
  "name": "Weekend City Break",
  "description": "Optimized for 2–3 night urban trips with mixed dining and sightseeing",
  "base_items": [
    {"item": "smart_casual_top", "category": "clothing", "quantity_formula": "min(duration, 3)", "tags": ["urban", "all_season"]},
    {"item": "jeans", "category": "clothing", "quantity": 1, "tags": ["urban", "all_season"]},
    {"item": "walking_shoes", "category": "footwear", "quantity": 1, "tags": ["urban", "comfort"]},
    {"item": "toiletry_bag_tsa", "category": "toiletries", "quantity": 1, "tags": ["carry_on_essential"]}
  ],
  "modifiers": {
    "cold_weather": [{"item": "light_jacket", "quantity": 1}],
    "rain_gear": [{"item": "compact_umbrella", "quantity": 1}],
    "business_dinner": [{"item": "blazer", "quantity": 1, "gender_specific": true}]
  }
}
```

**Template Categories:**
- Clothing (tops, bottoms, layers, underwear, sleepwear)
- Footwear (walking, formal, activity-specific)
- Toiletries (TSA-compliant, full-size if checked bag)
- Tech (chargers, adapters, portable battery)
- Documents (passport, tickets, insurance)
- Health (meds, first aid)
- Activity-specific (hiking gear, beach gear, business tech)

---

## 6. Output: Final Packing List

**Structure:**
```json
{
  "trip_id": "uuid",
  "user_id": "uuid",
  "generated_at": "2026-08-14T14:30:00Z",
  "template_used": "cultural_city_break",
  "bag_assignments": {
    "carry_on": ["toiletry_bag_tsa", "portable_battery", "meds", "documents"],
    "checked": ["hiking_boots", "blazer", "extra_clothing"]
  },
  "items": [
    {
      "item": "quick_dry_top",
      "category": "clothing",
      "quantity": 4,
      "bag": "checked",
      "reason": "duration=7, laundry=true, climate=humid"
    },
    {
      "item": "slip_on_loafers",
      "category": "footwear",
      "quantity": 1,
      "bag": "carry_on",
      "reason": "cultural_override: remove_shoes_temples"
    }
  ],
  "warnings": [
    "You marked 'blazer' unused on 3 past business trips—consider skipping",
    "Forecast shows 40% rain—umbrella added"
  ]
}
```

---

## 7. LLM Usage (Optional)

**Core logic is 100% deterministic.** Use LLM only for:

1. **Free-text activity parsing:**  
   - Input: "Exploring temples and hiking in the mountains"  
   - Output: `["temple_visits", "day_hike"]` with `venue_type` tags

2. **Natural-language explanations:**  
   - "You're going to Kyoto in spring—add light layers for temple visits and slip-on shoes for ryokan stays"

3. **Post-trip feedback analysis:**  
   - Input: "I never wore my dress shoes—they were too heavy"  
   - Output: Extract `item: dress_shoes`, `reason: too_heavy`, `action: downweight`

4. **Edge-case destinations:**  
   - If `country_code` not in cultural database, LLM suggests based on training data (flag as "unverified")

---

## 8. Implementation Checklist

- [ ] Build **archetype scoring function** (activities, accommodation, duration)
- [ ] Integrate **weather API** (e.g., OpenWeatherMap) for climate modifiers
- [ ] Create **template library** (JSON/YAML) with base items + modifiers
- [ ] Implement **quantity scaling formulas** (duration, laundry, bag limits)
- [ ] Add **user preference overrides** (style, must-haves, bag constraints)
- [ ] Build **cultural rules database** (by country code + venue overrides)
- [ ] Optional: LLM layer for free-text activity parsing + feedback analysis
- [ ] Design UI for user profile setup + trip input + list review
- [ ] Add post-trip feedback loop (mark items used/unused, reason)

---

## 9. Corollary Documents

This spec is supported by the following rule-set documents:

1. **`rule_sets_templates.md`** – Trip archetypes and template definitions
2. **`rule_sets_cultural.md`** – Cultural norms by destination + venue overrides
3. **`rule_sets_user_profile.md`** – User profile schema + preference logic
4. **`rule_sets_packing_suggestions.md`** – Item-level suggestions + quantity formulas

---

## 10. References

- Travel packing complaints analysis [web:17][web:23][web:27][web:28]
- Template structures and best practices [web:6][web:11][web:40][web:42][web:43]
- Cultural etiquette guidelines [web:44][web:45][web:46][web:49][web:52][web:55][web:58]
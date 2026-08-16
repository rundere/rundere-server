# Rule Sets: Cultural Norms by Destination

**Version:** 1.0  
**Date:** August 14, 2026  
**Purpose:** Define cultural indicators and overrides by destination (country/region) + venue type.

---

## 1. Data Structure

Each country/region entry follows this schema:

```json
{
  "country_code": "JP",
  "country_name": "Japan",
  "region": "East Asia",
  "base_rules": {
    "dress_code_general": "conservative_neat",
    "dress_code_temples": "modest_shoulders_knees",
    "dress_code_business": "formal_suit",
    "shoes_off_locations": ["homes", "ryokan", "temples", "traditional_restaurants"],
    "taboos": ["tipping", "loud_public_behavior", "eating_while_walking"],
    "greeting": "bow",
    "footwear_note": "slip_on_shoes_recommended"
  },
  "venue_overrides": {
    "temple": {
      "dress_code": "modest_shoulders_knees",
      "footwear": "remove_shoes",
      "add_items": ["slip_on_shoes", "scarf_for_shoulders"],
      "behavior": "quiet_respectful"
    },
    "beach": {
      "dress_code": "swimwear_acceptable",
      "remove_items": ["formal_wear"],
      "note": "cover_up_when_leaving_beach"
    },
    "restaurant_formal": {
      "dress_code": "smart_casual_to_formal",
      "add_items": ["blazer"]
    }
  },
  "seasonal_flags": {
    "cherry_blossom_season": {
      "months": [3, 4],
      "adjustments": ["crowded_temples", "suggest_minimal_bags"]
    }
  },
  "event_flags": {
    "golden_week": {
      "dates": "late_April_to_early_May",
      "adjustments": ["crowded_transport", "book_ahead"]
    }
  }
}
```

---

## 2. Cultural Rules by Country (Top 50 Destinations)

### 2.1 Japan (JP)

**Base Rules:**
- Dress code: Conservative neat (avoid overly casual or revealing clothing)
- Temples/shrines: Modest dress (cover shoulders and knees), remove shoes
- Ryokan/traditional inns: Remove shoes, often provided slippers
- Business: Formal suit expected
- Taboos: Tipping, loud behavior, eating while walking, pointing with fingers

**Venue Overrides:**
- `temple`: Modest shoulders/knees, remove shoes, add slip-on shoes + scarf
- `beach`: Swimwear OK, but cover up when leaving beach
- `ryokan`: Remove shoes, pack socks (no holes), avoid heavy perfume

**Seasonal Flags:**
- Cherry blossom season (Mar–Apr): Crowded temples → suggest minimal bags, slip-on shoes
- Golden Week (late Apr–early May): Extremely crowded → pack light, book ahead

**Packing Impact:**
- Add: Slip-on loafers, light cardigan (for temples), modest scarf
- Remove: High heels (impractical for temple removal), overly casual tank tops

---

### 2.2 United Arab Emirates (AE)

**Base Rules:**
- Dress code: Modest in public (cover shoulders and knees for both genders)
- Beaches/resorts: Swimwear acceptable, but cover up when leaving
- Mosques: Full modesty (women: headscarf, full coverage; men: long pants)
- Business: Formal, conservative (suits for men, modest dresses/suits for women)
- Taboos: PDA, revealing clothing in public, eating/drinking in public during Ramadan daylight

**Venue Overrides:**
- `mosque`: Full coverage (headscarf for women, long pants for men), remove shoes
- `beach`: Swimwear OK, but pack cover-up (kaftan, long shirt)
- `mall`: Modest dress enforced (no sleeveless tops, short shorts)
- `desert_safari`: Casual, breathable, but still modest (cover shoulders/knees)

**Seasonal Flags:**
- Ramadan (lunar calendar, shifts yearly): Dress more conservatively, avoid eating/drinking in public during daylight

**Packing Impact:**
- Add: Loose, breathable tops (long sleeves), long skirts/pants, headscarf (women), light cardigan
- Remove: Tank tops, short shorts, revealing beachwear for city days

---

### 2.3 Thailand (TH)

**Base Rules:**
- Dress code: Modest at temples (cover shoulders/knees), casual elsewhere
- Temples/palaces: Strict modesty (no sleeveless, no shorts above knees), remove shoes
- Beaches: Swimwear OK, but cover up in towns
- Business: Smart casual to formal (lightweight fabrics due to heat)
- Taboos: Touching heads, pointing feet at people/Buddha images, disrespecting monarchy

**Venue Overrides:**
- `temple`: Modest shoulders/knees, remove shoes, add scarf/sarong
- `beach`: Swimwear OK, pack cover-up for town
- `night_market`: Casual, comfortable shoes, light clothing

**Packing Impact:**
- Add: Light scarf/sarong (for temple coverage), slip-on shoes, breathable fabrics
- Remove: Heavy jackets, formal shoes (unless business)

---

### 2.4 France (FR)

**Base Rules:**
- Dress code: Neat, polished (avoid overly casual/sporty in cities)
- Restaurants: Smart casual (no athletic wear, flip-flops in upscale places)
- Churches: Modest dress (cover shoulders/knees)
- Business: Formal (suits, dresses)
- Taboos: Not greeting before asking questions, loud behavior in public

**Venue Overrides:**
- `church`: Modest shoulders/knees, remove hats
- `fine_dining`: Smart casual to formal (blazer, dress shoes)
- `beach` (South of France): Swimwear OK, but cover up in towns

**Packing Impact:**
- Add: Blazer, smart shoes, light scarf (for churches)
- Remove: Athletic wear for city days, flip-flops for restaurants

---

### 2.5 India (IN)

**Base Rules:**
- Dress code: Modest (cover shoulders/knees, especially for women)
- Temples/mosques/gurdwaras: Full modesty, remove shoes, sometimes head cover
- Business: Formal (suits for men, modest dresses/suits for women)
- Taboos: Eating with left hand, touching people with feet, public displays of affection

**Venue Overrides:**
- `temple`: Modest shoulders/knees, remove shoes, add headscarf (sometimes required)
- `gurdwara`: Full head cover (carry scarf), remove shoes, cover arms/legs
- `beach` (Goa): Swimwear OK, but cover up elsewhere

**Packing Impact:**
- Add: Loose, breathable tops (long sleeves), long skirts/pants, headscarf, slip-on shoes
- Remove: Revealing clothing, short shorts, tank tops for city/temple days

---

### 2.6 Italy (IT)

**Base Rules:**
- Dress code: Polished, neat (Italians dress well even casually)
- Churches/Vatican: Modest dress (cover shoulders/knees, no hats)
- Restaurants: Smart casual (no athletic wear, flip-flops in upscale places)
- Business: Formal (suits, dresses)
- Taboos: Not greeting, wearing hats in churches, overly casual attire in cities

**Venue Overrides:**
- `church`: Modest shoulders/knees, remove hats, add scarf if needed
- `fine_dining`: Smart casual to formal (blazer, dress shoes)
- `beach` (Amalfi, etc.): Swimwear OK, but cover up in towns

**Packing Impact:**
- Add: Blazer, smart shoes, light scarf (for churches)
- Remove: Athletic wear for city days, flip-flops for restaurants

---

### 2.7 Morocco (MA)

**Base Rules:**
- Dress code: Modest (cover shoulders/knees, especially in rural areas)
- Mosques (non-Muslims cannot enter most, but dress modestly nearby)
- Medinas/markets: Modest, comfortable shoes (cobblestones)
- Business: Formal, conservative
- Taboos: PDA, revealing clothing, eating with left hand

**Venue Overrides:**
- `mosque_exterior`: Modest dress (cover shoulders/knees)
- `medina`: Modest, comfortable walking shoes, cross-body bag (pickpockets)
- `desert`: Casual, breathable, but still modest (cover shoulders/knees)

**Packing Impact:**
- Add: Loose, breathable tops (long sleeves), long skirts/pants, comfortable walking shoes, cross-body bag
- Remove: Revealing clothing, short shorts, tank tops

---

### 2.8 South Korea (KR)

**Base Rules:**
- Dress code: Neat, conservative (avoid overly casual or revealing)
- Temples/palaces: Modest dress (cover shoulders/knees), remove shoes
- Business: Formal (suits, dresses)
- Taboos: Writing names in red, sticking chopsticks upright in rice, age hierarchy disrespect

**Venue Overrides:**
- `temple`: Modest shoulders/knees, remove shoes, add slip-on shoes
- `business`: Formal suit, polished shoes
- `cafe culture`: Smart casual (Koreans dress well for cafes)

**Packing Impact:**
- Add: Slip-on shoes, light cardigan, smart casual outfits
- Remove: Overly casual athletic wear, revealing clothing

---

### 2.9 Saudi Arabia (SA)

**Base Rules:**
- Dress code: Very modest (abaya required for women in public, thobe for men)
- Mosques: Full coverage (women: abaya + headscarf; men: thobe)
- Business: Formal, conservative (thobe for men, abaya for women)
- Taboos: PDA, revealing clothing, eating/drinking in public during Ramadan daylight, alcohol

**Venue Overrides:**
- `mosque`: Full coverage (abaya + headscarf for women, thobe for men)
- `business`: Formal (thobe/abaya or conservative Western suit)
- `resort` (Red Sea): Swimwear OK within resort, but cover up elsewhere

**Packing Impact:**
- Add: Abaya (women), thobe (men, or rent), headscarf (women), conservative Western clothing
- Remove: Revealing clothing, shorts, sleeveless tops

---

### 2.10 United States (US)

**Base Rules:**
- Dress code: Casual to smart casual (varies by region)
- Churches: Modest dress (cover shoulders/knees in some)
- Business: Business casual to formal (depends on industry)
- Taboos: Varies by region (e.g., tipping expected, PDA acceptable in most places)

**Venue Overrides:**
- `church`: Modest shoulders/knees (in conservative regions)
- `business`: Business casual (blazer, chinos/dress) or formal (suit)
- `beach`: Swimwear OK, cover up in towns

**Packing Impact:**
- Add: Blazer (for business), smart casual outfits
- Remove: Overly formal wear for casual regions (e.g., Hawaii)

---

## 3. Regional Summaries (For Unlisted Countries)

### 3.1 Southeast Asia (TH, VN, ID, MY, PH, etc.)

- **General:** Modest at temples (cover shoulders/knees), casual elsewhere
- **Beaches:** Swimwear OK, cover up in towns
- **Business:** Smart casual to formal (lightweight fabrics)
- **Taboos:** Varies (e.g., head touching in Thailand, left hand in Indonesia)

### 3.2 Middle East (AE, SA, QA, OM, etc.)

- **General:** Very modest (cover shoulders/knees, loose clothing)
- **Mosques:** Full coverage (headscarf for women)
- **Business:** Formal, conservative
- **Taboos:** PDA, revealing clothing, eating/drinking in public during Ramadan

### 3.3 Europe (FR, IT, ES, DE, UK, etc.)

- **General:** Neat, polished (avoid overly casual in cities)
- **Churches:** Modest dress (cover shoulders/knees)
- **Business:** Formal (suits, dresses)
- **Taboos:** Varies (e.g., not greeting in France, hats in churches in Italy)

### 3.4 South Asia (IN, PK, BD, LK, etc.)

- **General:** Modest (cover shoulders/knees, especially for women)
- **Temples/mosques:** Full modesty, remove shoes, sometimes head cover
- **Business:** Formal (suits, modest dresses)
- **Taboos:** Eating with left hand, touching heads, public affection

### 3.5 East Asia (JP, KR, CN, TW, etc.)

- **General:** Neat, conservative (avoid overly casual or revealing)
- **Temples/palaces:** Modest dress, remove shoes
- **Business:** Formal (suits, dresses)
- **Taboos:** Varies (e.g., tipping in Japan, age hierarchy in Korea)

---

## 4. Seasonal & Event Flags

### 4.1 Ramadan (Muslim-Majority Countries)

- **Months:** Lunar calendar (shifts ~11 days earlier each year)
- **Adjustments:** Dress more conservatively, avoid eating/drinking in public during daylight
- **Packing Impact:** Add light, breathable long sleeves; pack snacks for private consumption

### 4.2 Cherry Blossom Season (Japan)

- **Months:** March–April
- **Adjustments:** Crowded temples → suggest minimal bags, slip-on shoes
- **Packing Impact:** Add compact day bag, slip-on loafers

### 4.3 Oktoberfest (Germany)

- **Dates:** Late September–early October
- **Adjustments:** Casual/traditional wear (lederhosen/dirndl) acceptable
- **Packing Impact:** Add comfortable shoes, casual outfits

### 4.4 Golden Week (Japan)

- **Dates:** Late April–early May
- **Adjustments:** Extremely crowded → pack light, book ahead
- **Packing Impact:** Add minimal bag, comfortable walking shoes

---

## 5. Implementation Notes

### 5.1 Data Sources

- Government travel advisories (e.g., US State Department, UK FCDO)
- Lonely Planet, Rough Guides, cultural etiquette databases [web:44][web:45][web:46]
- User feedback (flag incorrect/outdated rules)

### 5.2 Update Frequency

- **Major updates:** Quarterly (review top 50 destinations)
- **Event-driven:** As needed (e.g., new dress code laws in Saudi Arabia)
- **User feedback loop:** Allow users to flag incorrect rules → review within 48 hours

### 5.3 Strictness Levels

- **Strict mode (default for first-time travelers):** Enforce all cultural norms
- **Flexible mode (experienced travelers):** Suggest but don't enforce
- **Custom mode:** User overrides (e.g., "I always cover knees" or "I avoid headscarves")

---

## 6. Example: Cultural Override Application

**Trip:** Kyoto, Japan (JP), 7 days  
**Activities:** temple_visits, urban_walking, business_dinner  
**User:** Female, cultural_strictness = strict

**Base Rules (JP):**
- Dress code: Conservative neat
- Temples: Modest shoulders/knees, remove shoes
- Business: Formal suit

**Venue Overrides:**
- `temple`: Modest shoulders/knees, remove shoes, add slip-on shoes + scarf
- `restaurant_formal`: Smart casual to formal, add blazer

**Final Cultural Constraints:**
- Must cover shoulders and knees at temples
- Remove shoes at temples/ryokan
- Pack slip-on loafers (for easy removal)
- Add light cardigan/scarf (for temple coverage)
- Add blazer (for business dinner)

**Packing List Adjustments:**
- Add: Slip-on loafers, light cardigan, modest scarf, blazer
- Remove: High heels (impractical for temple removal), tank tops

---

## 7. References

- Cultural etiquette guidelines [web:44][web:45][web:46][web:49][web:52][web:55][web:58]
- Travel etiquette by country [web:47][web:48][web:50][web:53][web:54][web:56][web:57]
import Foundation

// Codable mirrors of the FastAPI `/api/v1` response models (app/api_v1.py) and
// the auth token flow (app/authn_routes.py). Field names match the JSON exactly,
// so no CodingKeys gymnastics: snake_case in, snake_case here.
// Timestamps that Pydantic emits as ISO-8601 (`created_at`, `captured_at`,
// `generated_at`) are decoded as String — the UI formats them — to stay robust
// against fractional-seconds / timezone variations.

// MARK: - Auth

struct UserPublic: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let username: String
    let is_admin: Bool
    let auth_source: String
    let traveler_id: Int?
    // 4.1 account + sharing fields (YONDER-101). Optional so a pre-4.1 server
    // or a user cached by an older build still decodes.
    let email: String?
    let share_with_admins: Bool?
    /// True when the server wants the first-sign-in sharing question asked.
    let sharing_prompt: Bool?
    /// Why opting out of admin sharing isn't possible yet ("" = it is).
    let opt_out_blocker: String?
}

struct TokenResponse: Codable, Sendable {
    let access: String
    let refresh: String
    let token_type: String
    let expires_in: Int
    let user: UserPublic
}

// MARK: - Dashboard

struct NextTrip: Codable, Hashable, Sendable {
    let id: String
    let name: String
    let start: String?
    let end: String?
    let in_progress: Bool
    let days_until: Int?
}

/// One moment on the viewer's timeline (server 4.7.1, YONDER-177, D-172): an
/// activity's start or end. `at` is its wall-clock time as stored; `label` is
/// what it's called ("Check-in", "Drop-off", "Departure").
struct MomentOut: Codable, Hashable, Sendable {
    let edge: String              // "start" | "end"
    let at: String
    let label: String
    let trip_id: String
    let trip_name: String?
    let activity: ActivityOut
}

struct Dashboard: Codable, Sendable {
    let next_trip: NextTrip?
    /// D-172: the soonest moment still ahead, and what's under way now (each as
    /// its end moment). Optional: pre-4.7.1 servers and older caches omit them.
    let next_activity: MomentOut?
    let in_progress: [MomentOut]?
    let counts: [String: Int]
    let capture: [String: Int]
    let trip_count: Int
    let upcoming_count: Int
}

// MARK: - Trips

struct TripSummary: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let start_date: String?
    let end_date: String?
    let source: String
    let owner_id: String?
    let activity_count: Int
    /// The airport codes the trip's flights visit, in order (server 4.7.1,
    /// YONDER-177). Optional: older servers and caches omit it.
    let airports: [String]?
}

struct ActivityOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let type: String
    let starts_at: String?
    let values: [String: JSONValue]
    // Server-rendered view of `values` (same registry as the web timeline).
    // All optional: older servers and older cached snapshots omit them.
    let type_label: String?
    let title: String?
    let subtitle: String?
    let ends_at: String?
    let start_label: String?
    let end_label: String?
    /// Which of the trip's travellers this is for (`TravelerOut.id`).
    /// Empty or nil = everyone on the trip.
    let traveler_ids: [Int]?
    let fields: [ActivityFieldOut]?
    let map_query: String?
    let map_url: String?
    /// Where it is (4.3): located points and, for a road trip, the route line.
    /// nil until the server has located it (and from pre-4.3 servers/caches).
    let geo: ActivityGeo?
}

/// An activity's located points (from/to/via, or the single place) and a road
/// trip's route — `public_geo` in app/trips/geo.py.
struct ActivityGeo: Codable, Hashable, Sendable {
    struct Point: Codable, Hashable, Sendable {
        /// "from" | "to" | "via" | "place"
        let role: String
        let label: String?
        let lat: Double
        let lon: Double
    }
    struct Route: Codable, Hashable, Sendable {
        /// Encoded polyline, precision 6 (OSRM/Valhalla "polyline6").
        let polyline: String?
        let distance_km: Double?
        let duration_minutes: Double?
    }
    let points: [Point]
    let route: Route?
}

struct ActivityFieldOut: Codable, Hashable, Sendable {
    let key: String
    let label: String
    let value: JSONValue
    let kind: String?
}

struct TravelerOut: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let display_name: String
    let role: String
    let kind: String
}

struct PlaceOut: Codable, Hashable, Sendable {
    let key: String?
    let label: String?
    let city: String?
    let country: String?
    let lat: Double?
    let lon: Double?
}

struct FindingOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let topic: String
    let place_key: String?
    let body_md: String?
    let data: JSONValue?
    let confidence: String?
    let status: String
    let sources: JSONValue?
    /// Approved/edited on a lockable topic — Discover runs won't overwrite it.
    let locked: Bool?
    /// Written by an external AI agent over MCP/API, named by `agent`.
    let external: Bool?
    let agent: String?
}

struct PackingItemOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let item_key: String
    let label: String
    let category: String
    let quantity: Int
    let bag: String?
    var status: String          // draft | packed (removed lines aren't sent)
    let reason: String?
}

struct PackingOut: Codable, Hashable, Sendable {
    let generated_at: String?
    let archetypes: JSONValue?
    let warnings: JSONValue?
    let tasks: JSONValue?
    var items: [PackingItemOut]
}

struct CaptureLite: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let kind: String
    let headline: String?
    let created_at: String?
}

struct TripSnapshot: Codable, Hashable, Sendable {
    let trip: TripSummary
    let activities: [ActivityOut]
    let travelers: [TravelerOut]
    let places: [PlaceOut]
    let discovery: [FindingOut]
    /// Discovery 2.0 (server 4.5): the Food & Drink / Events & Activities rows,
    /// and the person's likes/dislikes. Optional: older servers and caches.
    let discovery_items: [DiscoveryItemOut]?
    let discovery_profile: JSONValue?
    var packing: PackingOut?
    let captures: [CaptureLite]
}

/// The caller's own Discovery run on a trip (YONDER-238): POST
/// /trips/{id}/discover starts it (optionally targeted, see DiscoverIn) (409 on an ended trip, D-157), GET
/// /trips/{id}/discover/status polls it. Reload the trip once `running` is false.
struct DiscoveryStatusOut: Codable, Hashable, Sendable {
    let running: Bool
    let phase: String?
    let done: Int
    let total: Int
    let pct: Int
    let ended: Bool
    let ended_message: String?
    /// running | done | failed | cancelled; nil = never run.
    let last_status: String?
    let last_started_at: String?
    let last_finished_at: String?
    /// What the latest run covered: nil = the whole suite, else one topic (and
    /// one place) — a targeted refresh.
    let last_topic: String?
    let last_place_key: String?
}

/// POST /trips/{id}/discover body — omit it for the whole suite, or refresh
/// one `topic` ("weather"), or one topic for one place ("PT:Lisbon", as on the
/// finding). Unknown topic or a place without a topic → 400.
struct DiscoverIn: Codable, Hashable, Sendable {
    let topic: String?
    let place_key: String?
}

/// A place, event or activity inside a Food & Drink or Events & Activities
/// finding (server 4.5, YONDER-147). `finding_id` ties it to its city.
struct DiscoveryItemOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let finding_id: String
    let topic: String            // food | events
    let place_key: String
    let kind: String             // restaurant|cafe|bar|…|event|activity
    let name: String
    let neighbourhood: String?
    let address: String?
    let lat: Double?
    let lon: Double?
    let website: String?
    let what_to_get: String?
    let price_tier: Int?
    let starts_on: String?
    let ends_on: String?
    let when_text: String?
    let venue: String?
    let details: String?
    let duration: String?
    let booking: Bool?
    let sources: JSONValue?
    let extra: JSONValue?
    let origin: String?
    let agent: String?
    let hidden: Bool?
    let hidden_reason: String?
    let edited: Bool?
    /// Set once it's on the timeline.
    let activity_id: String?
}

/// POST /api/v1/trips/{id}/discovery-items/{item}/timeline (server 4.5).
struct ItemToTimelineIn: Codable, Sendable {
    var starts_at: String?        // YYYY-MM-DDTHH:MM local; nil = the event's date
    var traveler_ids: [Int]?      // nil / [] = everyone
    var notes: String?
}

// MARK: - Review

struct ReviewItemOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let bucket: String
    let kind: String?
    let headline: String?
    let subline: String?
    let reference: String?
    let note: String?
    let reason_codes: [String]
    let blocked: Bool
    let confidence: Double?
    let created_at: String?
}

struct ReviewList: Codable, Sendable {
    let counts: [String: Int]
    /// Captured but not processed yet (the funnel's first stage).
    let to_process: Int?
    let items: [ReviewItemOut]
}

// MARK: - Trip editing (write, YONDER-103)
// Request bodies are named after their server schemas so dto_drift.py checks
// them. Optional fields are omitted when nil = "leave unchanged".

struct TripPatch: Codable, Sendable {
    var name: String?
    var start_date: String?      // "" clears
    var end_date: String?
    var notes: String?
}

struct ActivityIn: Codable, Sendable {
    var type: String
    var values: [String: JSONValue]
    var starts_at: String?
    var traveler_ids: [Int]?     // nil / [] = everyone on the trip
}

struct ActivityPatch: Codable, Sendable {
    var type: String?
    var values: [String: JSONValue]?   // replaces the whole dict
    var starts_at: String?
    var traveler_ids: [Int]?
}

struct PackingItemPatch: Codable, Sendable {
    var status: String?          // draft | packed | removed
    var quantity: Int?
    var bag: String?
    var label: String?
}

struct PackingItemIn: Codable, Sendable {
    var label: String
    var category: String = "activity"
    var quantity: Int = 1
    var bag: String?
}

/// A timeline activity type and the `values` fields it takes
/// (GET /api/v1/activity-types) — the activity editor is built from these.
struct ActivityTypeOut: Codable, Hashable, Sendable {
    let key: String
    let label: String
    let fields: [ActivityTypeField]
}

struct ActivityTypeField: Codable, Hashable, Sendable {
    let key: String
    let label: String
    let type: String             // text | textarea | url | datetime-local
    let hint: String?
}

// MARK: - Review writes (YONDER-78; server side YONDER-57)

/// GET /api/v1/review/{id}: everything the review screen shows and edits.
struct ReviewItemDetail: Codable, Identifiable, Sendable {
    let id: String
    let bucket: String
    let kind: String?
    let headline: String?
    let subline: String?
    let reference: String?
    let note: String?
    let reason_codes: [String]
    let blocked: Bool
    let confidence: Double?
    let created_at: String?
    let action_hint: String?
    let stages: JSONValue?
    let fields: [ReviewFieldOut]
    let locked_fields: [String]
    let violations: [String]
    let writes: JSONValue?
    let collection_name: String?
    let trip_id: String?
    /// Flights: "Who flew", from the Travellers list (never free text).
    let passengers: [TravelerOptionOut]?
    let passenger_names_unmatched: [String]
    /// Others filed to a 2+ traveller trip: "Who is this for".
    let who: [TravelerOptionOut]?
    /// Trips the caller can FILE into (server's filing rule).
    let trip_options: [TripOptionOut]
    let can_approve: Bool
    let can_unapprove: Bool
    let can_ignore: Bool
}

/// One editable field of a capture (same specs as the web detail pane).
struct ReviewFieldOut: Codable, Hashable, Sendable {
    let label: String
    let path: String             // JSON pointer; ReviewPatch.fields key
    let kind: String             // text | datetime | select
    let value: String
    let tz: String?
    let precision: String?       // datetime: date | datetime
    let options: [String]?
    let missing: Bool            // required and empty — why it can't log yet
    let editable: Bool
}

struct TravelerOptionOut: Codable, Identifiable, Hashable, Sendable {
    let id: String               // passengers: identity id; who: traveller id
    let name: String
    let kind: String?
    let selected: Bool
}

struct TripOptionOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let start_date: String?
    let end_date: String?
    let contains: Bool           // the trip's dates contain the capture's
    let selected: Bool
}

struct ReviewPatch: Codable, Sendable {
    var fields: [String: String]?
    var passengers: [String]?
    var traveler_ids: [Int]?
    var approve: Bool = false
}

struct ReviewActionOut: Codable, Sendable {
    let ok: Bool
    let message: String?
    let changed: [String]
    let item: ReviewItemDetail
}

struct AssignTripIn: Codable, Sendable {
    let trip_id: String
}

struct CommitIn: Codable, Sendable {
    var dry_run: Bool
}

/// A background job (process / commit), polled at GET /api/v1/jobs/{id}.
struct JobOut: Codable, Sendable {
    let id: String
    let kind: String
    let phase: String?
    let done: Int
    let total: Int
    let current: String?
    let finished: Bool
    let error: String?
    let toast: String?
    let details: [String]
}

/// POST /api/v1/review/commit {dry_run: true}: the exact plan, nothing written.
/// (Not a declared response model on the server — see SKIP in dto_drift.py.)
struct CommitPreview: Codable, Sendable {
    struct Summary: Codable, Sendable {
        let items: Int
        let writes_ok: Int
        let writes_failed: Int
        let skipped: Int
        let plans: [Plan]
    }
    struct Plan: Codable, Sendable, Identifiable {
        let item_id: String
        let headline: String?
        let results: [Result]
        var id: String { item_id }
    }
    struct Result: Codable, Sendable, Hashable {
        let target: String
        let operation: String?
        let ok: Bool?
        let skipped: Bool?
        let error: String?
    }
    let summary: Summary
}

// MARK: - Capture (write)

struct CaptureIn: Codable, Sendable {
    var url: String?
    var html: String?
    var travel_date: String?
    var travel_time: String?
    var end_date: String?
    var timezone: String?
    var mode: String?
    var collection_id: String?
    /// A Rundere trip to file it under — pinned, like a manual pick in Review (4.3).
    var trip_id: String?
    var title: String?
    var notes: String?

    init(url: String? = nil, html: String? = nil, travel_date: String? = nil,
         travel_time: String? = nil, end_date: String? = nil, timezone: String? = nil,
         mode: String? = nil, collection_id: String? = nil, trip_id: String? = nil,
         title: String? = nil, notes: String? = nil) {
        self.url = url; self.html = html; self.travel_date = travel_date
        self.travel_time = travel_time; self.end_date = end_date; self.timezone = timezone
        self.mode = mode; self.collection_id = collection_id; self.trip_id = trip_id
        self.title = title; self.notes = notes
    }
}

struct CaptureOut: Codable, Sendable {
    let batch_id: String
    let status: String
}

// MARK: - Travel Journal (SPEC-6; own-only, YONDER-79)

struct JournalOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    /// The day the entry is ABOUT ("YYYY-MM-DD"), not when it was typed (D-132).
    let entry_date: String
    let captured_at: String?
    let trip_id: String?
    let body: String
    let values: [String: JSONValue]?
    let place_lat: Double?
    let place_lon: Double?
    let place_label: String?
    let source: String?
    let duplicated_from: String?
}

struct JournalCreate: Codable, Sendable {
    var entry_date: String?
    var captured_at: String?     // device time, ISO-8601
    var body: String
    var trip_id: String?
    var place_lat: Double?
    var place_lon: Double?
    var place_label: String?
    var source: String = "ios"
}

/// PATCH body. The trip link only changes with `attach_trip` = true, and then
/// `trip_id` must be sent even when nil (explicit null = detach, D-133).
struct JournalPatch: Codable, Sendable {
    var entry_date: String?
    var body: String?
    var trip_id: String?
    var attach_trip: Bool = false

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(entry_date, forKey: .entry_date)
        try c.encodeIfPresent(body, forKey: .body)
        if attach_trip {
            try c.encode(trip_id, forKey: .trip_id)   // null detaches
            try c.encode(true, forKey: .attach_trip)
        }
    }
}

// MARK: - Error envelope

struct APIErrorBody: Codable, Sendable {
    let detail: String?
}

// MARK: - Expenses (server 4.8, SPEC-10, YONDER-172)
// Amounts and rates are decimal strings ("42.50"), as the server sends them, so
// no float rounding creeps into who owes whom. The server does the arithmetic.

struct ExpenseShareOut: Codable, Hashable, Sendable {
    let traveler_id: Int
    let name: String
    let amount: String               // in the expense's currency
}

struct ExpenseOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let trip_id: String
    let kind: String                 // expense | settlement
    let title: String
    let amount: String
    let currency: String
    let rate: String?                // trip currency per 1 unit; nil = unconverted
    let rate_source: String?         // same | entered | discover
    let amount_in_trip_currency: String?
    let spent_on: String?
    let payer_id: Int
    let payer_name: String
    let split: String                // equal | exact
    let shares: [ExpenseShareOut]
    let category: String
    let activity_id: String?
    /// The planned item this paid for (D-181). Optional: early 4.8 builds omit it.
    let budget_item_id: String?
    let notes: String?
    let owner_id: String?
    let created_at: String?
    /// The discussion on it, oldest first (D-185). Optional: earlier 4.8 builds omit it.
    let comments: [ExpenseCommentOut]?
}

struct ExpenseCommentOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let author_id: String?
    let author_name: String
    let body: String
    let created_at: String?
    let mine: Bool                   // you wrote it (and can delete it)
}

struct ExpenseCommentIn: Codable, Sendable {
    var body: String
}

/// A planned cost (D-178/D-179/D-184): one estimated amount for the travellers
/// tagged on it — never multiplied by people or days (a room for two is one cost).
struct BudgetItemOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let trip_id: String
    let title: String
    let category: String
    let amount: String               // the estimate, in `currency`
    let currency: String
    /// Who it's for (D-184). Optional: earlier 4.8 builds omit these.
    let traveler_ids: [Int]?
    let traveler_names: [String]?
    let start_date: String?
    let end_date: String?            // when it applies, if not the whole trip
    let total: String?               // trip currency; nil = no rate
    let rate: String?
    let rate_source: String?
    let status: String               // draft (an agent's suggestion) | kept
    let place_key: String?
    let activity_id: String?
    let notes: String?
    let sources: [JSONValue]
    let source: String
    let actual: String               // logged against it so far (trip currency)
    let owner_id: String?
}

struct CategoryPlanOut: Codable, Hashable, Sendable {
    let category: String
    let planned: String
    let drafts: String
    let actual: String
}

/// One traveller's budget beside their share of actual spending (D-184).
struct TravelerPlanOut: Codable, Hashable, Sendable {
    let traveler_id: Int
    let name: String
    let planned: String
    let actual: String
}

/// The budget beside what was spent. Only actuals settle up (D-178).
struct PlanOut: Codable, Sendable {
    let people: Int
    let days: Int?                   // nil = the trip has no dates yet
    let planned: String
    let drafts: String
    let actual: String
    let items: [BudgetItemOut]
    let by_category: [CategoryPlanOut]
    let by_traveler: [TravelerPlanOut]?
    let unconverted: [String]
}

struct BalanceOut: Codable, Hashable, Sendable {
    let traveler_id: Int
    let name: String
    let paid: String
    let share: String
    let net: String                  // + is owed money, − owes money
}

struct TransferOut: Codable, Hashable, Sendable {
    let from_id: Int
    let from_name: String
    let to_id: Int
    let to_name: String
    let amount: String
}

struct ExpensesOut: Codable, Sendable {
    let currency: String?            // nil until the first expense
    let total: String
    let expenses: [ExpenseOut]
    let balances: [BalanceOut]
    let transfers: [TransferOut]
    let unconverted: [String]
    let categories: [String]
    /// The planned side of Costs (D-178). Optional: early 4.8 builds omit it.
    let plan: PlanOut?
}

struct ExpenseIn: Codable, Sendable {
    var amount: String
    var currency: String
    var payer_id: Int
    var title: String = ""
    var spent_on: String?
    var split: String = "equal"
    var traveler_ids: [Int]?         // equal split; nil/[] = everyone on the trip
    var shares: [String: String]?    // exact split: traveller id (as text) → amount
    var category: String = "other"
    var rate: String?                // blank = the server uses your Discover rates
    var activity_id: String?
    var budget_item_id: String?      // the planned item it fulfils (D-181)
    var notes: String?
    var source: String = "ios"
}

/// Only the fields set are sent (nil keys are omitted by JSONEncoder).
struct ExpensePatch: Codable, Sendable {
    var amount: String?
    var currency: String?
    var payer_id: Int?
    var title: String?
    var spent_on: String?
    var split: String?
    var traveler_ids: [Int]?
    var shares: [String: String]?
    var category: String?
    var rate: String?
    var activity_id: String?
    var budget_item_id: String?      // "" unlinks
    var notes: String?
}

struct BudgetItemIn: Codable, Sendable {
    var amount: String
    var currency: String
    var title: String = ""
    var category: String = "other"
    var traveler_ids: [Int]?         // who it's for; nil = you (D-184)
    var start_date: String?
    var end_date: String?
    var rate: String?
    var notes: String?
    var source: String = "ios"
}

/// Editing a planned item keeps it; `status: "kept"` keeps a draft as it is.
struct BudgetItemPatch: Codable, Sendable {
    var amount: String?
    var currency: String?
    var title: String?
    var category: String?
    var traveler_ids: [Int]?
    var start_date: String?          // "" clears it
    var end_date: String?
    var rate: String?
    var notes: String?
    var status: String?
}

struct SettlementIn: Codable, Sendable {
    var from_id: Int
    var to_id: Int
    var amount: String
    var currency: String?
    var spent_on: String?
    var source: String = "ios"
}

struct ExpenseCurrencyIn: Codable, Sendable {
    var currency: String
}

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
    /// The person's own currency (4.13): the calculator converts into it first.
    let base_currency: String?
    // Travel themes (5.0, D-237 to D-240); nil on a 4.x server, which draws Base.
    /// The stored choice. After 5.0 it may be `random` or `next-trip`.
    var ui_theme: String? = nil
    /// What to draw: the server has already resolved the choice into a theme.
    var resolved_theme: String? = nil
    /// The scenery floor switch (D-239); the server's default is on.
    var scenery: Bool? = nil
}

/// PATCH /api/v1/auth/me: send only what changes.
struct AppearanceBody: Codable, Sendable {
    var ui_theme: String?
    var scenery: Bool?
}

/// GET /api/v1/themes: every theme the server offers (D-240).
struct ThemesOut: Codable, Hashable, Sendable {
    let version: Int
    let themes: [ThemeOut]
}

struct ThemeOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    /// base | travel
    let kind: String
    /// Bumped when the theme's values change.
    let version: Int
    var description: String? = nil
    var tags: [String]? = nil
    /// `light` and `dark`.
    var modes: [String: ThemeModeOut]? = nil
    /// Nil for Base, which has no scenery.
    var scene: ThemeSceneOut? = nil
}

struct ThemeModeOut: Codable, Hashable, Sendable {
    /// The fifteen colour roles, by name (`text-secondary`), as `#RRGGBB` or `rgba()`.
    var color: [String: String]? = nil
    var effect: [String: String]? = nil
    var scene: [String: String]? = nil
}

struct ThemeSceneOut: Codable, Hashable, Sendable {
    var title: String? = nil
    let light_url: String
    let dark_url: String
    /// D-245 (server 5.0): the same scenes as transparent PNG at 2x and 3x, for
    /// a theme newer than the build, since iOS can't draw a fetched SVG.
    /// Optional: older servers omit them.
    var light_png_url: String? = nil
    var dark_png_url: String? = nil
    var light_png_3x_url: String? = nil
    var dark_png_3x_url: String? = nil
}

struct TokenResponse: Codable, Sendable {
    let access: String
    let refresh: String
    let token_type: String
    let expires_in: Int
    let user: UserPublic
}

/// How someone may sign in on the host that was asked (server 4.9.4, YONDER-180,
/// D-194). A 404 means an older server: password only.
struct AuthMethodsOut: Codable, Hashable, Sendable {
    let password: Bool
    let oauth: Bool
    /// The first-party OAuth client for the app; nil = password only.
    let oauth_client_id: String?
    let sso: Bool
    let sso_name: String?
    let sso_forced: Bool?
    let registration_open: Bool?
    let server_name: String?
    /// D-198 (server 4.12): the public read-only demo. Show its banner; every
    /// trip also reports `my_role` viewer. Optional: older servers omit it.
    let demo: Bool?
    let version: String
    /// D-244 (server 5.0): what this server can do, by stable name. Absent on a
    /// server before 5.0, which means none of the names in `ServerCapability`.
    var capabilities: [String]? = nil
}

/// The capability names the app gates on (server `app/capabilities.py`). Names
/// are never renamed or reused; the app ignores names it doesn't know.
enum ServerCapability {
    static let tripPDFFlags = "trip_pdf_flags"
    static let dataExport = "data_export"
    static let accountDelete = "account_delete"
    static let themeScenePNG = "theme_scene_png"
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
    /// YONDER-250 (server 4.12): the next moments within `window_h` (default 48 h,
    /// up to `limit`, default 12) across every trip, soonest first; the first is
    /// `next_activity`. Optional: pre-4.12 servers omit it.
    let upcoming: [MomentOut]?
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
    /// The trip's notes (server 4.10, YONDER-239). nil = none, or an older server.
    let notes: String?
    /// Whether you may change the trip (4.10). nil from older servers: assume yes,
    /// as before, and let the server refuse.
    let can_edit: Bool?
    /// D-199 (server 4.12, YONDER-185): owner | traveller | admin | viewer.
    /// `viewer` = a view-only share; the demo server reports it on every trip.
    /// Optional: pre-4.12 servers omit it.
    let my_role: String?
}

extension TripSummary {
    /// May this person change the trip? `can_edit` when the server says, else
    /// the role; older servers say neither: assume yes and let the server refuse.
    var isEditable: Bool {
        if let can_edit { return can_edit }
        return my_role != "viewer"
    }
    var isViewOnly: Bool { !isEditable }
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
    /// D-199: true = has a Run·dere login; false = a guest on the roster only.
    let has_account: Bool?
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
    /// Whose list this is (5.0): always the signed-in person's own name. Nil on
    /// an older server or when there's no list.
    var for_name: String? = nil
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
    /// The trip's shared, dated exchange rates (4.10, D-195). Optional: older servers.
    let trip_rates: [TripRateOut]?
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
    /// The IANA zone to show `captured_at` in (D-224): the zone it was written
    /// in, else the zone at its coordinates. Nil when unknown or on servers
    /// before 4.13.4.
    var captured_tz: String? = nil
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
    /// The device's IANA zone when the entry was written (D-224), so it reads
    /// as the writer's clock did. Queued entries keep it (outbox replay).
    var captured_tz: String?
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

// MARK: - Release notes (server 5.0, D-234, RUNDEREIOS-4)

struct ReleaseNotesVersionOut: Codable, Identifiable, Hashable, Sendable {
    let version: String
    /// YYYY-MM-DD; nil for a version that isn't out yet.
    let date: String?
    /// Short Markdown items: **bold**, `code` and https links only.
    let everyone: [String]
    /// Setup notes for admins; always empty for anyone else.
    let admins: [String]
}

extension ReleaseNotesVersionOut {
    var id: String { version }
}

struct ReleaseNotesOut: Codable, Hashable, Sendable {
    let running_version: String
    /// The last version whose notes the person closed, on any device or the web.
    let seen_version: String?
    let versions: [ReleaseNotesVersionOut]
}

// MARK: - Error envelope

struct APIErrorBody: Codable, Sendable {
    let detail: String?
    /// A machine-readable reason, e.g. `packing_profile_required` (5.0).
    var code: String? = nil
    /// A page on the server that fixes it, as a path.
    var url: String? = nil
    /// Sign-in refused because the account is waiting to be deleted (5.0, D-242).
    var deletion_pending: Bool? = nil
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
    /// Where it was spent (YONDER-212, 4.8.2): set once when it was added.
    let place_lat: Double?
    let place_lon: Double?
    let place_label: String?
    /// The day its rate is for (4.10, D-195); nil = same currency or no rate.
    let rate_date: String?
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
    /// The currency picker's suggestions, best first, and what a new entry
    /// starts in (YONDER-213, 4.8.2). Optional for older servers.
    let suggested_currencies: [String]?
    let default_currency: String?
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
    /// Where it was spent (YONDER-212): captured on add, never on edit.
    var place_lat: Double?
    var place_lon: Double?
    var place_accuracy_m: Double?
    var place_label: String?
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

// MARK: - Saved places (YONDER-200, server 4.8.2, D-166–D-169)

/// Save a place from a Google / Apple Maps place link; the server parses it.
struct SavedPlaceIn: Codable, Sendable {
    var url: String
    var notes: String?
    var source: String = "ios"
    // What MapKit found on the phone (YONDER-344, D-167). Server 4.13 prefers
    // these over its own parse; older servers ignore them.
    var name: String?
    var lat: Double?
    var lon: Double?
    var address: String?
    var locality: String?
    var region: String?
    var country_code: String?
    var category: String?
    // Sort it as it's saved (5.0, D-236); older servers ignore both. The kind
    // only applies to a place the save adds or brings back, and lists are
    // added to, never replaced.
    var activity_type: String?
    var list_ids: [String]?
}

/// One of the person's own lists of places (5.0, D-213). A place's `list_ids`
/// point to these.
struct PlaceListOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    var name: String
    var emoji: String?
    var description: String?
    /// Lists are shown lowest first, then oldest first.
    var position: Int?
    /// How many library places it holds.
    var count: Int?
    let created_at: String?
    let updated_at: String?
}

extension PlaceListOut {
    /// "🕯️ Date night", or the name alone.
    var title: String { [emoji, name].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " ") }
}

struct PlaceListIn: Codable, Sendable {
    var name: String
    var emoji: String?
    var description: String?
}

/// PATCH /api/v1/places/lists/{id}: only the fields you send change.
struct PlaceListPatch: Codable, Sendable {
    var name: String?
    var emoji: String?
    var description: String?
    var position: Int?
}

/// Suggestions for the Places search box (5.0, D-235): put an item's `insert`
/// where `q[start..<end]` was.
struct PlaceSuggestOut: Codable, Hashable, Sendable {
    let start: Int
    let end: Int
    let items: [PlaceSuggestionOut]
}

struct PlaceSuggestionOut: Codable, Hashable, Sendable {
    /// chip | kind | list | city | country
    let kind: String
    let label: String
    let insert: String
    /// Add a space after it; false for a chip that still wants its value (`kind:`).
    let complete: Bool
    /// How many of the person's places it matches; nil for a chip.
    let count: Int?
}

extension SavedPlaceIn {
    /// The link plus whatever MapKit was sure of (YONDER-344).
    mutating func adopt(_ m: PlaceResolver.Match?) {
        guard let m else { return }
        name = m.name
        lat = m.lat
        lon = m.lon
        address = m.address
        locality = m.locality
        country_code = m.countryCode
        category = m.category
        activity_type = m.kind
    }
}

struct SavedPlaceOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let lat: Double?
    let lon: Double?
    let address: String?
    let category: String?
    /// What it becomes on a timeline (dining, tour…).
    let activity_type: String
    let provider: String
    let source_url: String?
    let notes: String?
    let source: String?
    let created_at: String?
    /// saved | filed | dismissed
    let status: String?
    /// The trip its location and dates point to, if any.
    let suggested_trip_id: String?
    /// created | revived | duplicate | near_duplicate | already_filed | removed_earlier
    let outcome: String?
    // Server 4.13 (SPEC-11 phase 1); nil on older servers.
    let locality: String?
    let region: String?
    let country_code: String?
    let continent: String?
    /// What to do there.
    let todo: String?
    /// someday | must
    let priority: String?
    /// YYYY-MM-DD
    let visited_on: String?
    /// The trips it's on (a place can be on several).
    let trip_ids: [String]?
    let list_ids: [String]?
    let updated_at: String?
}

/// PATCH /api/v1/places/{id}: only the fields you send change.
struct SavedPlacePatch: Codable, Sendable {
    var name: String?
    var activity_type: String?
    var address: String?
    var locality: String?
    var region: String?
    var country_code: String?
    var notes: String?
    var todo: String?
    var priority: String?
    var visited_on: String?
}

/// File a saved place to a trip. All optional: no date = undated on the timeline.
struct SavedPlaceFileIn: Codable, Sendable {
    var starts_at: String?
    var activity_type: String?
    var notes: String?
    var traveler_ids: [Int]?
}

// MARK: - Capture channels (YONDER-240)

/// Your own email-forward address (SPEC-9 D-162). `enabled` is false when the
/// server's inbox isn't set up.
struct InboxAddressOut: Codable, Sendable {
    let enabled: Bool
    let address: String?
}

// MARK: - Discovery runs (server 4.10, YONDER-238)

/// POST /api/v1/trips/{id}/discover. Empty = the whole suite; `topic` (and
/// `place_key`) = just that one.
struct DiscoverIn: Codable, Sendable {
    var topic: String?
    var place_key: String?
}

/// The caller's own Discovery run on a trip (runs are per person) — what the
/// Run / Refresh button and its progress bar poll.
struct DiscoveryStatusOut: Codable, Hashable, Sendable {
    let running: Bool
    let phase: String?
    let done: Int
    let total: Int
    let pct: Int
    /// The trip has ended: no new runs (D-157), and why.
    let ended: Bool
    let ended_message: String?
    let last_status: String?
    let last_started_at: String?
    let last_finished_at: String?
    let last_topic: String?
    let last_place_key: String?
}

/// YONDER-264 (server 4.12, Refine Discovery): GET /trips/{id}/discover/places
/// lists the places a run would research; PUT …/places/{key} with
/// `DiscoveryPlaceIn` skips or restores one. Both need edit access.
struct DiscoveryPlaceOut: Codable, Hashable, Sendable {
    let key: String               // stable place key, e.g. "PT:Lisbon"
    let label: String?
    let city: String?
    let country: String?
    let lat: Double?
    let lon: Double?
    /// Where the place came from, e.g. "flight · to".
    let sources: [String]
    /// A home place: never researched.
    let is_home: Bool
    /// In the person's home country: local topics are skipped.
    let home_country: Bool
    let excluded: Bool
}

extension DiscoveryPlaceOut: Identifiable {
    var id: String { key }
}

struct DiscoveryPlacesOut: Codable, Hashable, Sendable {
    let places: [DiscoveryPlaceOut]
    let countries: [String]
    /// Places named on the trip that couldn't be verified: shown, not offered.
    let dropped: [String]
}

struct DiscoveryPlaceIn: Codable, Sendable {
    let excluded: Bool
}

// MARK: - Trip exchange rates (server 4.10, D-195, YONDER-237/242)

/// One row of the trip's shared rate table: 1 `currency` = `rate` `quote` for
/// `rate_date`; `as_of` is the day the feed published it.
struct TripRateOut: Codable, Hashable, Sendable {
    let currency: String
    let quote: String
    let rate_date: String
    let rate: String
    let as_of: String?
    let source: String               // frankfurter | discover
    let fetched_at: String?
}

/// POST /api/v1/trips/{id}/rates/{code}/refresh: the rate it stored, and the
/// expenses it converted with it.
struct RateRefreshOut: Codable, Sendable {
    let rate: TripRateOut
    let applied: [String]
}

// MARK: - Documents (server 4.11, YONDER-179, SPEC-8 D-196)
// Kept the same as the server's contracts/ios/DTOs.swift, which added them first.

/// A file you captured — PDF, .ics or .eml. Only its uploader ever sees it.
struct DocumentOut: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let filename: String
    let content_type: String         // application/pdf | text/calendar | message/rfc822
    let size_bytes: Int
    let trip_id: String?
    let batch_id: String?
    let source: String
    let scan_notes: String?
    let created_at: String?
    /// When the stored copy is removed (12 months after its trip ends).
    let removed_on: String?
    let purged: Bool
}

/// GET /documents — your files and storage. `enabled` false = this server
/// doesn't take files (hide the file actions; `reason` says why).
struct DocumentsOut: Codable, Hashable, Sendable {
    let enabled: Bool
    let reason: String?
    let used_bytes: Int
    let quota_bytes: Int
    let max_file_bytes: Int
    let documents: [DocumentOut]
}

/// POST /capture/file (multipart: file, trip_id?, notes?, source="ios") →
/// 202. 413 too big / over quota, 415 not PDF/.ics/.eml, 422 unsafe, 503 off
/// or the scanner is down. `duplicate` = you'd already captured this file.
struct FileCaptureOut: Codable, Hashable, Sendable {
    let document: DocumentOut
    let batch_id: String?
    let duplicate: Bool
}

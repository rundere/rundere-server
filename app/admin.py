"""SQLAdmin over the models (D-18: the raw table explorer; ~30 lines of
config). Reached only via the internal Traefik router in production. The
RuleSetting view is the D-25 per-rule management UI."""
import rules as rules_module
import wtforms
from sqladmin import Admin, ModelView

from app import models
from discover.resolvers.language import PHRASE_KEYS as _PHRASE_KEYS


class LinesField(wtforms.TextAreaField):
    """A JSON list edited as one value per line.

    The column stores a list, but asking someone to hand-write
    ["A","B"] in a text box is a needless trap — an unbalanced quote silently
    becomes a validation error about a type they never chose to care about."""

    def _value(self):
        if self.raw_data:
            return self.raw_data[0]
        return "\n".join(self.data or [])

    def process_formdata(self, valuelist):
        raw = valuelist[0] if valuelist else ""
        self.data = [line.strip() for line in raw.splitlines() if line.strip()]


def _theme(admin) -> None:
    """Give the admin the review queue's visual identity (D-30).

    Rather than forking SQLAdmin's templates, register the packaged ones under
    a "_sqladmin" prefix so our override can `extend` the original and add a
    stylesheet in its head_tail block. Nothing breaks when SQLAdmin updates.
    """
    from pathlib import Path

    from jinja2 import ChoiceLoader, FileSystemLoader, PackageLoader, PrefixLoader

    overrides = Path(__file__).parent / "review" / "admin_templates"
    shared = Path(__file__).parent / "review" / "templates"
    env = admin.templates.env
    env.loader = ChoiceLoader([
        FileSystemLoader(str(overrides)),
        PrefixLoader({"_sqladmin": PackageLoader("sqladmin", "templates")}),
        # Last, and only reached for names the two above don't define — so the
        # admin can {% include %} the shared global header (_appheader.html)
        # without any base.html/layout.html name clashing with SQLAdmin's own.
        FileSystemLoader(str(shared)),
        env.loader,
    ])


def _registry_field(row, field: str):
    """rule_settings rows carry only rule_id + mode; everything human-readable
    comes from the rules.py registry at render time. A row with no registry
    entry (renamed/removed rule) shows a warning rather than a blank cell."""
    rule = rules_module.RULES.get(getattr(row, "rule_id", None))
    if rule is None:
        return "— no registry entry (renamed or removed rule?)"
    if field == "critical":
        return "YES — encodes a past real-world failure" if rule.critical else ""
    return getattr(rule, field, "")


def mount_admin(app, engine) -> None:
    admin = Admin(app, engine, title="Run·dere")
    _theme(admin)

    # 4.0 Phase 2 (D-124): the admin UI surfaces ONLY platform administration and
    # the traveller roster (for linking a user to a traveller). NO personal travel
    # data — captures (TravelItem), ingest batches, commit attempts, item keys,
    # artifacts, trips, or trip activities — is exposed here. Those remain in the
    # database only. Travel is deeply personal; an admin administers the platform,
    # they do not browse anyone's trips (owner privacy directive, 2026-08-28).

    class SourceRuleAdmin(ModelView, model=models.SourceRule):
        column_list = [models.SourceRule.name, models.SourceRule.enabled,
                       models.SourceRule.position]

    class ApiKeyAdmin(ModelView, model=models.ApiKey):
        column_list = [models.ApiKey.client_name, models.ApiKey.created_at,
                       models.ApiKey.revoked_at, models.ApiKey.last_used_at]
        column_details_exclude_list = [models.ApiKey.key_hash]
        can_create = False          # creation happens via CLI so the plaintext
                                    # is shown exactly once

    class RuleSettingAdmin(ModelView, model=models.RuleSetting):
        name = "Rule Setting"
        name_plural = "Rule Settings"
        # The table stores only rule_id + mode; the human-readable text lives
        # in the rules.py registry (D-25: rules are code, enablement is data).
        # Join them here so the screen is usable without cross-referencing
        # RULES.md — a bare list of rule IDs tells the operator nothing.
        column_list = ["rule_id", "group", "summary", "critical",
                       models.RuleSetting.mode, models.RuleSetting.note,
                       models.RuleSetting.updated_at]
        column_labels = {"rule_id": "Rule", "group": "Group",
                         "summary": "What it checks", "critical": "Critical",
                         "mode": "Mode", "note": "Note",
                         "updated_at": "Updated"}
        column_details_list = column_list
        column_searchable_list = [models.RuleSetting.rule_id,
                                  models.RuleSetting.mode]
        column_sortable_list = [models.RuleSetting.rule_id,
                                models.RuleSetting.mode]
        column_default_sort = "rule_id"
        page_size = 100                     # all 57 rules on one screen
        can_create = can_delete = False     # rows come from the registry (D-25)

        column_formatters = {
            "group": lambda m, _: _registry_field(m, "group"),
            "summary": lambda m, _: _registry_field(m, "summary"),
            "critical": lambda m, _: _registry_field(m, "critical"),
        }
        column_formatters_detail = column_formatters

    class TravelerIdentityAdmin(ModelView, model=models.TravelerIdentity):
        name = "Traveller"
        name_plural = "Travellers"
        # D-124: the admin roster is a LINKING tool only — name + kind. Personal
        # traveller attributes a user entered (nationality, aliases, notes,
        # AirTrail mapping) are NOT shown here; each user edits their own at
        # Travel profile (/trips/settings/travel-profile). D-35: exactly one row
        # is kind="self".
        column_list = [models.TravelerIdentity.display_name,
                       models.TravelerIdentity.kind]
        column_labels = {"display_name": "Name", "kind": "Kind"}
        form_columns = ["display_name", "kind"]
        # `kind` is a closed vocabulary (self | guest, D-35) — force the choice.
        form_overrides = {"kind": wtforms.SelectField}
        form_args = {
            "kind": {"choices": [("self", "self — the AirTrail account holder"),
                                 ("guest", "guest")],
                     "default": "guest",
                     "description": "Exactly one traveller is 'self' (the "
                     "AirTrail account holder); everyone else is a guest."},
        }

    class PackingCatalogItemAdmin(ModelView, model=models.PackingCatalogItem):
        name = "Packing Item"
        name_plural = "Packing Items"
        # The owner-editable packing item catalog (D-84): seeded from
        # planning/data/items.json, then managed HERE instead of by editing JSON.
        # archetypes.json references these ids, so retire an item with `enabled`
        # rather than deleting it out from under a template reference.
        column_list = [models.PackingCatalogItem.id, models.PackingCatalogItem.label,
                       models.PackingCatalogItem.category, models.PackingCatalogItem.bag,
                       models.PackingCatalogItem.weight_g,
                       models.PackingCatalogItem.enabled,
                       models.PackingCatalogItem.tags]
        column_labels = {"id": "Item id", "weight_g": "Weight (g)"}
        column_searchable_list = [models.PackingCatalogItem.id,
                                  models.PackingCatalogItem.label,
                                  models.PackingCatalogItem.category]
        column_sortable_list = [models.PackingCatalogItem.category,
                                models.PackingCatalogItem.label]
        column_default_sort = "category"
        page_size = 200
        # Edit the human fields. `id` is auto-derived from the label on create
        # (its slug, e.g. "Car Phone Mount" -> car_phone_mount) — SQLAdmin drops
        # the PK from the create form, so listing it here would just insert NULL.
        # `variants`/`updated_at` stay off the form.
        form_columns = ["label", "category", "bag", "weight_g", "tags", "enabled"]
        form_overrides = {"tags": LinesField, "category": wtforms.SelectField,
                          "bag": wtforms.SelectField}
        form_args = {
            "tags": {"description": "One tag per line (e.g. modest, warm, formal)."},
            "category": {"choices": [(c, c) for c in
                                     ("clothing", "footwear", "toiletries", "tech",
                                      "documents", "health", "activity")]},
            # Single-select on purpose: anything that fits carry-on also fits
            # checked, so carry-on is the safe default for a new item.
            "bag": {"choices": [("carry_on", "carry-on"), ("checked", "checked")],
                    "default": "carry_on"},
        }

    class BagAdmin(ModelView, model=models.Bag):
        name = "Bag"
        name_plural = "Bag Inventory"
        # The owner's actual bags (3 backpacks, a carry-on, a checked suitcase —
        # with real capacities) — parallel to and independent from the packing
        # catalog above (what goes IN a bag). Seeded empty on purpose (unlike the
        # catalog, there's no universal default bag inventory). Once bags exist
        # here, the per-trip packing item "bag" dropdown lists THESE instead of
        # the generic carry-on/checked/backpack categories. Retire a bag you no
        # longer own with `enabled` rather than deleting it — a PackingItem
        # still pointing at a deleted (not just disabled) row falls back to
        # showing the raw id, same as an orphaned PackingCatalogItem reference.
        column_list = [models.Bag.label, models.Bag.kind, models.Bag.capacity,
                       models.Bag.enabled]
        column_searchable_list = [models.Bag.label, models.Bag.kind]
        column_sortable_list = [models.Bag.kind, models.Bag.label]
        column_default_sort = "kind"
        page_size = 200
        # `id` auto-derives from the label on create (its slug), same as the
        # packing catalog — SQLAdmin drops the PK from the create form.
        form_columns = ["label", "kind", "capacity", "enabled"]
        form_overrides = {"kind": wtforms.SelectField}
        form_args = {
            "kind": {"choices": [("backpack", "backpack"), ("suitcase", "suitcase"),
                                 ("personal_item", "personal item")],
                     "default": "backpack"},
            "capacity": {"description": 'Freeform, e.g. "40L" or "22in carry-on".'},
        }

    # NB: PackingProfile has NO SQLAdmin view on purpose — it's edited through the
    # structured form at /trips/settings/packing-profile (Administer → Packing
    # profile), not a raw-JSON table (D-86).

    class PhrasebookPhraseAdmin(ModelView, model=models.PhrasebookPhrase):
        name = "Phrasebook Phrase"
        name_plural = "Phrasebook"
        # The editable traveller phrasebook (3.6): seeded from phrases.json, then
        # managed here. One row per (language, phrase key). `source` marks vetted
        # vs generated — flip a generated row to `vetted` to PROMOTE an
        # LLM-generated phrasebook into the trusted set. `id` auto-derives from
        # lang_code:phrase_key on create (SQLAdmin drops the PK from the form).
        column_list = [models.PhrasebookPhrase.lang_code,
                       models.PhrasebookPhrase.phrase_key,
                       models.PhrasebookPhrase.text,
                       models.PhrasebookPhrase.pronunciation,
                       models.PhrasebookPhrase.source,
                       models.PhrasebookPhrase.enabled,
                       models.PhrasebookPhrase.updated_at]
        column_labels = {"lang_code": "Language", "phrase_key": "Phrase",
                         "pronunciation": "Pronunciation"}
        column_searchable_list = [models.PhrasebookPhrase.lang_code,
                                  models.PhrasebookPhrase.phrase_key,
                                  models.PhrasebookPhrase.source]
        column_sortable_list = [models.PhrasebookPhrase.lang_code,
                                models.PhrasebookPhrase.phrase_key,
                                models.PhrasebookPhrase.source]
        column_default_sort = "lang_code"
        page_size = 200
        form_columns = ["lang_code", "phrase_key", "text", "pronunciation",
                        "source", "enabled"]
        form_overrides = {"phrase_key": wtforms.SelectField,
                          "source": wtforms.SelectField}
        form_args = {
            "lang_code": {"description": "ISO language code, e.g. hi, kn, de."},
            "phrase_key": {"choices": [(k, f"{k} — {lbl}")
                                       for k, lbl in _PHRASE_KEYS]},
            "text": {"description": "The phrase in the language's own script."},
            "pronunciation": {"description": "Simple romanised pronunciation."},
            "source": {"choices": [("vetted", "vetted — trusted"),
                                   ("generated", "generated — from the LLM, review")],
                       "default": "vetted",
                       "description": "Flip generated → vetted to promote it."},
        }

    class RegionalLanguageAdmin(ModelView, model=models.RegionalLanguage):
        name = "Regional Language"
        name_plural = "Regional Languages"
        # The editable region→language map (3.6): seeded from
        # regional_languages.json. One row per (country, region, language) — the
        # regional resolver surfaces these for a city in that region, beyond the
        # national official set. Region is matched case-insensitively.
        column_list = [models.RegionalLanguage.country,
                       models.RegionalLanguage.region,
                       models.RegionalLanguage.lang_code,
                       models.RegionalLanguage.enabled,
                       models.RegionalLanguage.updated_at]
        column_labels = {"country": "Country (ISO-2)", "region": "Region/state",
                         "lang_code": "Language"}
        column_searchable_list = [models.RegionalLanguage.country,
                                  models.RegionalLanguage.region,
                                  models.RegionalLanguage.lang_code]
        column_sortable_list = [models.RegionalLanguage.country,
                                models.RegionalLanguage.region]
        column_default_sort = "country"
        page_size = 200
        form_columns = ["country", "region", "lang_code", "enabled"]
        form_args = {
            "country": {"description": "ISO-2 country code, e.g. IN, ES, CA."},
            "region": {"description": "State/province name as geocoded, "
                       "e.g. Karnataka (matched case-insensitively)."},
            "lang_code": {"description": "ISO language code, e.g. kn, ca, fr."},
        }

    # Platform administration + the traveller roster only (D-124). ApiKey stays
    # (revoking a compromised ingest key is platform admin, and keys are minted
    # by CLI); it never shows the key itself, only client name + status.
    for view in (SourceRuleAdmin, ApiKeyAdmin, TravelerIdentityAdmin,
                 RuleSettingAdmin,
                 PackingCatalogItemAdmin, BagAdmin, PhrasebookPhraseAdmin,
                 RegionalLanguageAdmin):
        admin.add_view(view)

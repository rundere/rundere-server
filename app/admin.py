"""SQLAdmin over the models (D-18: the raw table explorer; ~30 lines of
config). Reached only via the internal Traefik router in production. The
RuleSetting view is the D-25 per-rule management UI."""
import rules as rules_module
import wtforms
from sqladmin import Admin, ModelView

from app import models


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
    admin = Admin(app, engine, title="travel-extractor")
    _theme(admin)

    class TravelItemAdmin(ModelView, model=models.TravelItem):
        column_list = [models.TravelItem.id, models.TravelItem.item_type,
                       models.TravelItem.canonical_key, models.TravelItem.batch_id,
                       models.TravelItem.created_at]
        can_create = False          # items enter via the pipeline, not the admin

    class ItemKeyAdmin(ModelView, model=models.ItemKey):
        column_list = [models.ItemKey.item_id, models.ItemKey.key, models.ItemKey.kind]

    class IngestBatchAdmin(ModelView, model=models.IngestBatch):
        column_list = [models.IngestBatch.id, models.IngestBatch.status,
                       models.IngestBatch.received_at,
                       models.IngestBatch.declared_segment_count]
        can_create = False

    class CommitAttemptAdmin(ModelView, model=models.CommitAttempt):
        column_list = [models.CommitAttempt.item_id, models.CommitAttempt.target,
                       models.CommitAttempt.response_status,
                       models.CommitAttempt.attempted_at]
        can_create = can_edit = False       # verbatim evidence (D-19)

    class SourceRuleAdmin(ModelView, model=models.SourceRule):
        column_list = [models.SourceRule.name, models.SourceRule.enabled,
                       models.SourceRule.position]

    class ApiKeyAdmin(ModelView, model=models.ApiKey):
        column_list = [models.ApiKey.client_name, models.ApiKey.created_at,
                       models.ApiKey.revoked_at, models.ApiKey.last_used_at]
        column_details_exclude_list = [models.ApiKey.key_hash]
        can_create = False          # creation happens via CLI so the plaintext
                                    # is shown exactly once

    class ArtifactAdmin(ModelView, model=models.Artifact):
        column_list = [models.Artifact.sha256, models.Artifact.content_type,
                       models.Artifact.size_bytes, models.Artifact.created_at]
        can_create = can_edit = can_delete = False      # immutable (D-15)

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
        # D-35: exactly one row is kind="self" (the AirTrail account holder);
        # everyone else is a guest. aliases is a JSON list edited one-per-line
        # (LinesField) because airlines print names in shapes no two agree on.
        column_list = [models.TravelerIdentity.display_name,
                       models.TravelerIdentity.kind,
                       models.TravelerIdentity.airtrail_user_id,
                       models.TravelerIdentity.aliases]
        column_labels = {"display_name": "Name", "kind": "Kind",
                         "airtrail_user_id": "AirTrail user id",
                         "aliases": "Aliases"}
        form_overrides = {"aliases": LinesField}
        form_args = {"aliases": {"description":
                     "One alias per line (name as printed on tickets)."}}

    for view in (TravelItemAdmin, ItemKeyAdmin, IngestBatchAdmin,
                 CommitAttemptAdmin, SourceRuleAdmin, ApiKeyAdmin,
                 ArtifactAdmin, TravelerIdentityAdmin,
                 RuleSettingAdmin):
        admin.add_view(view)

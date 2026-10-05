"""Controlled file planning and application scoped to the framework's node storage."""

import json
import sqlite3
from bloxsmith_app.block_api import APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimeResult
from . import logic, ui


# FB1 - Plan bounded copies, moves and quarantine without mutating the selected tree.
# FB2 - Apply only exact fresh approvals, recheck sources and never overwrite destinations.
# FB3 - Preserve durable receipts, refuse replay and report partial outcomes without retry.
# FB4 - Route request/decision events through both runtimes and canonical block storage.
# FB5 - Provide translated responsive modal and inspector settings with preserved drafts.
class FileOperationsBlock(BlockDefinition):
    """An input is a proposal, never implicit approval to mutate files."""

    kind = "file_operations"

    def ui_assets(self, surface="modal"):
        """Use the static release manifest in every installation mode."""
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def execute_runtime(self, context):
        """Consume new events, never replay a stale request when a decision arrives."""
        try:
            config = logic.configuration(context.config)
            if context.input_events:
                ports = {int(port.id): str(port.name) for port in context.input_ports}
                messages = [(ports.get(event.input_port_id), event.value) for event in context.input_events]
            else:
                messages = [(name, context.input_value(name, ident)) for name, ident in (("request", "1"), ("decision", "2"))
                            if context.has_input_value(name, ident)]
                if len(messages) > 1:
                    raise logic.FileError("Manual execution with two populated inputs is ambiguous; send one request or decision.")
            if not messages:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(messages) > 10:
                raise logic.FileError("Too many input events in one activation.")
            resolver = context.services.get("get_block_storage_dir")
            if not callable(resolver):
                raise logic.FileError("Persistent block storage is unavailable; no fallback directory is used.")
            directory = resolver()
            outputs = []
            for port, value in messages:
                values = logic.process(value, port, config, directory, context.root_dir,
                                       context.services.get("cancel_requested", lambda: False))
                outputs.extend(BlockRuntimeOutput(port_id=int(output.id), port_name=output.name,
                               value=json.dumps(values[output.name], ensure_ascii=False, allow_nan=False), content_type=APPLICATION_JSON)
                               for output in context.output_ports if output.name in values)
            return BlockRuntimeResult(status="success", outputs=outputs, content_type=APPLICATION_JSON,
                                      logs=["[file-operations] Plan or decision processed; inspect plan/receipt outputs."])
        except (ValueError, TypeError, RecursionError, RuntimeError, OSError, sqlite3.Error) as exc:
            detail = str(exc) if isinstance(exc, logic.FileError) else "The ledger or filesystem is unavailable. No automatic reset or retry is performed."
            message = self.translate("block.file_operations.error", {"detail": detail}, fallback="File operation failed: {detail}")
            return BlockRuntimeResult(status="failed", outputs=[], error=message, exit_code=1,
                                      logs=[f"[file-operations-error] {detail}"])

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Validate settings without opening the ledger or mutating files from a UI action."""
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    return {"error": self.translate("block.file_operations.error", {"detail": str(exc)}, fallback="File operation failed: {detail}")}
        result = super().handle_ui_action(node=node, action="modal_update_fields" if action == "save_settings" else action, values=values, payload=payload)
        return ui.replace_config(result, node)

    def render_modal(self, *, node, payload=None):
        """Render settings without altering or disclosing the stored records."""
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        """Expose the same bounded policy in the narrow inspector."""
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        """Show the two-stage policy, not source paths or private file data."""
        return ui.card(self, node, self.translate("block.file_operations.role", fallback="Plan before applying"))

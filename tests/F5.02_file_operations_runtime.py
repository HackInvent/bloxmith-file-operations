"""FB1/FB2/FB3/FB4/FB5: real plans and approved file copies across runs and package origins."""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.file_operations.block import FileOperationsBlock
from bloxsmith_app.block_api import BlockRuntimeContext
from block_test_packages import install_test_package, surface_payload
from ui_smoke_common import create_project_api, create_run_api, data_edge, display_node, graph_payload, graph_storage_dir, http_json, isolated_server, text_node, wait_for_run_terminal


def main():
    """Apply only an approval constructed by this test for its own disposable fixture."""
    block = FileOperationsBlock()
    assert block.execute_runtime(BlockRuntimeContext(config={})).status == "skipped"
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            with isolated_server() as server:
                tree = server.root_dir / "fixture-files"
                tree.mkdir()
                (tree / "source.txt").write_text("Approved test copy", encoding="utf-8")
                node = block.build_node_payload(node_id="files", position={"x": 360, "y": 140})
                node["config"] = {**block.default_config(), "allowed_root": str(tree)}
                if origin:
                    model = install_test_package(server, "file_operations", origin=origin)
                    node["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        html = surface_payload(server, model, node, surface=surface)["html"]
                        assert "{{" not in html
                        if surface != "node_card":
                            assert 'data-block-config-field="allowed_operations"' in html
                request = {"id": "fixture-1", "revision": 1,
                           "operations": [{"op": "copy", "source": "source.txt", "target": "approved.txt"}]}

                def document(value, input_port, output_port):
                    return graph_payload("Controlled files", [text_node("source", "Event", json.dumps(value), 80, 140),
                        node, display_node("display", "Result", 700, 140)], [
                        data_edge("event-files", "source", 1, "files", input_port),
                        data_edge("files-display", "files", output_port, "display", 1)])

                graph = document(request, 1, 1)
                project = create_project_api(server, document=graph)["project"]
                run = create_run_api(server, graph, project_id=project["graph_id"], runtime_mode=mode)
                result = wait_for_run_terminal(server, run["run_id"], timeout_sec=30)
                assert result["status"] == "success", (mode, origin, result.get("logs"))
                plan = json.loads(result["output_values"]["files:1"]["value"])
                assert not (tree / "approved.txt").exists() and (tree / "source.txt").exists()
                decision = {"id": plan["id"], "revision": plan["revision"], "plan_hash": plan["plan_hash"], "decision": "approve"}
                approved_graph = document(decision, 2, 2)
                # Seed this isolated fixture directly. Validate/reload in the actual server,
                # whose release registry owns managed/linked packages (not this test process).
                graph_file = graph_storage_dir(server, project) / "graph.json"
                graph_file.write_text(json.dumps({**approved_graph, "graph_id": project["graph_id"]}), encoding="utf-8")
                http_json(server.base_url, f"/api/projects/{project['graph_id']}/graph/reload", method="POST", payload={})
                run = create_run_api(server, approved_graph, project_id=project["graph_id"], runtime_mode=mode)
                result = wait_for_run_terminal(server, run["run_id"], timeout_sec=30)
                assert result["status"] == "success", (mode, origin, result.get("logs"))
                assert "files:2" in result["output_values"], (mode, origin, result.get("logs"), result.get("output_values"))
                receipt = json.loads(result["output_values"]["files:2"]["value"])
                assert receipt["status"] == "applied", receipt
                assert (tree / "approved.txt").read_text() == "Approved test copy"
                assert (tree / "source.txt").exists()
    print("[ok] File Operations exact approval across real runs and package origins")


if __name__ == "__main__":
    main()

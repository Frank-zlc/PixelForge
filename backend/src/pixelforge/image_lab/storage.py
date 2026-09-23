"""SQLite catalog migration and immutable local image imports."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import cv2
import numpy as np

from pixelforge.image_lab.operators import OPERATORS, OperatorSpec, manifest
from pixelforge.vision import tool_catalog

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 30_000_000
MAX_BATCH_FILES = 500
MAX_BATCH_BYTES = 512 * 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_hash(spec: OperatorSpec) -> str:
    """Hash the handler plus whichever tool_catalog helpers it actually calls.

    Most operators dispatch through the legacy ``process_image``/``IMPLEMENTATIONS``
    table; a few (template_match, match_verify) call named tool_catalog helpers
    directly instead. Either way, a version bump is required exactly when the code
    a spec actually runs changes -- not one fixed list for every operator.
    """
    source = inspect.getsource(spec.handler).encode()
    source += inspect.getsource(tool_catalog.demo_image).encode()
    if spec.id in tool_catalog.IMPLEMENTATIONS:
        source += inspect.getsource(tool_catalog.process_image).encode()
        source += inspect.getsource(tool_catalog.IMPLEMENTATIONS[spec.id]).encode()
    if spec.id in {"color_mask", "text_enhance", "match_verify"}:
        source += inspect.getsource(tool_catalog._color_mask).encode()
    if spec.id in {"template_match", "match_verify"}:
        source += inspect.getsource(tool_catalog.decode_data_url).encode()
    if spec.id == "template_match":
        source += inspect.getsource(tool_catalog.draw_marker).encode()
    if spec.id == "match_verify":
        source += inspect.getsource(tool_catalog.similarity_ratio).encode()
    return hashlib.sha256(source).hexdigest()


def _relative_name(value: str) -> str:
    if not value or len(value) > 1024 or "\\" in value or "\x00" in value:
        raise ValueError("invalid relative image name")
    name = PurePosixPath(value)
    if name.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("image name must be a relative path without parent segments")
    return str(name)


def _execute_ddl(db: sqlite3.Connection, statements: str) -> None:
    """Execute schema statements inside the caller's transaction."""
    for statement in statements.split(";"):
        if statement.strip():
            db.execute(statement)


class ImageLabStore:
    def __init__(self, database: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.files = database.parent / "image_lab"
        self.files.mkdir(parents=True, exist_ok=True)
        self._import_lock = threading.Lock()
        self._migrate()
        self._sync_builtin_tools()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _migrate(self) -> None:
        with closing(self.connect()) as db:
            has_catalog = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='image_tools'"
            ).fetchone() is not None
            migrated = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone() is not None
            if has_catalog and not migrated:
                backup = self.database.with_name(
                    f"{self.database.stem}.before-image-lab-{datetime.now(UTC):%Y%m%d%H%M%S}.sqlite3"
                )
                with closing(sqlite3.connect(backup)) as target:
                    db.backup(target)
            db.execute("BEGIN IMMEDIATE")
            with db:
                _execute_ddl(db, """
                    CREATE TABLE IF NOT EXISTS image_tools (
                        id TEXT PRIMARY KEY,
                        category TEXT NOT NULL,
                        name TEXT NOT NULL,
                        function_name TEXT NOT NULL,
                        description TEXT NOT NULL,
                        effect_image TEXT,
                        availability TEXT NOT NULL,
                        source TEXT NOT NULL,
                        parameters_json TEXT NOT NULL,
                        sort_order INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL,
                        checksum TEXT NOT NULL
                    );
                """)
                existing = {row["name"] for row in db.execute("PRAGMA table_info(image_tools)")}
                columns = {
                    "availability_reason": "TEXT",
                    "catalog_state": "TEXT",
                    "current_version": "TEXT",
                    "deprecated_by": "TEXT",
                    "created_at": "TEXT",
                    "updated_at": "TEXT",
                }
                for name, kind in columns.items():
                    if name not in existing:
                        db.execute(f"ALTER TABLE image_tools ADD COLUMN {name} {kind}")
                _execute_ddl(db, """
                    CREATE TABLE IF NOT EXISTS tool_versions (
                        id TEXT PRIMARY KEY,
                        tool_id TEXT NOT NULL REFERENCES image_tools(id),
                        version TEXT NOT NULL,
                        implementation_key TEXT NOT NULL,
                        params_schema_json TEXT NOT NULL,
                        inputs_schema_json TEXT NOT NULL,
                        outputs_schema_json TEXT NOT NULL,
                        code_hash TEXT NOT NULL,
                        dependency_json TEXT NOT NULL DEFAULT '{}',
                        published_at TEXT NOT NULL,
                        state TEXT NOT NULL,
                        UNIQUE(tool_id, version)
                    );
                    CREATE TABLE IF NOT EXISTS tool_examples (
                        id TEXT PRIMARY KEY,
                        tool_version_id TEXT NOT NULL REFERENCES tool_versions(id),
                        title TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        input_refs_json TEXT NOT NULL,
                        params_json TEXT NOT NULL,
                        roi_json TEXT,
                        result_refs_json TEXT NOT NULL,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        verified_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS image_assets (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        import_batch_id TEXT,
                        relative_name TEXT NOT NULL,
                        storage_path TEXT NOT NULL,
                        preview_path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        byte_size INTEGER NOT NULL,
                        width INTEGER NOT NULL,
                        height INTEGER NOT NULL,
                        channels TEXT NOT NULL,
                        orientation TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        deleted_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS notebooks (
                        id TEXT PRIMARY KEY,
                        project_id TEXT,
                        title TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS notebook_steps (
                        id TEXT PRIMARY KEY,
                        notebook_id TEXT NOT NULL REFERENCES notebooks(id),
                        position INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        tool_version_id TEXT REFERENCES tool_versions(id),
                        inputs_json TEXT NOT NULL,
                        params_json TEXT NOT NULL,
                        roi_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(notebook_id, position)
                    );
                    CREATE TABLE IF NOT EXISTS notebook_runs (
                        id TEXT PRIMARY KEY,
                        notebook_id TEXT NOT NULL REFERENCES notebooks(id),
                        notebook_revision INTEGER NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        snapshot_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        error_json TEXT,
                        idempotency_key TEXT
                    );
                    CREATE TABLE IF NOT EXISTS step_runs (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES notebook_runs(id),
                        step_id_snapshot TEXT NOT NULL,
                        position_snapshot INTEGER NOT NULL,
                        tool_version_id TEXT NOT NULL REFERENCES tool_versions(id),
                        resolved_inputs_json TEXT NOT NULL,
                        outputs_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        duration_ms INTEGER,
                        error_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_image_assets_batch
                        ON image_assets(import_batch_id, relative_name);
                    CREATE INDEX IF NOT EXISTS idx_notebook_steps_order
                        ON notebook_steps(notebook_id, position);
                    CREATE INDEX IF NOT EXISTS idx_notebook_runs_history
                        ON notebook_runs(notebook_id, started_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_step_runs_order
                        ON step_runs(run_id, position_snapshot);
                """)
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES (1, ?, ?)",
                    (_now(), hashlib.sha256(b"image-lab-schema-v1").hexdigest()),
                )

    def _sync_builtin_tools(self) -> None:
        with closing(self.connect()) as db, db:
            for order, spec in enumerate(OPERATORS.values()):
                description = spec.description
                details = spec.as_dict()
                version_id = f"{spec.id}@{spec.version}"
                existing_version = db.execute(
                    "SELECT code_hash FROM tool_versions WHERE id=?", (version_id,)
                ).fetchone()
                code_hash = _source_hash(spec)
                if existing_version is not None and existing_version["code_hash"] != code_hash:
                    details["availability"] = "planned"
                    details["status"] = "version_mismatch"
                    details["availability_reason"] = "实现代码已变化; 请发布新的工具版本"
                    details["effect_image"] = None
                example_inputs: dict[str, dict[str, str]] = {}
                example_results: dict[str, dict[str, str]] = {}
                if details["status"] == "ready":
                    example_inputs, example_results = self._materialize_demo(db, spec)
                    details["effect_image"] = example_results["image"]["url"]
                db.execute(
                    """INSERT INTO image_tools
                    (id, category, name, function_name, description, effect_image, availability,
                     source, parameters_json, sort_order, availability_reason, catalog_state,
                     current_version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      category=excluded.category, name=excluded.name,
                      function_name=excluded.function_name, description=excluded.description,
                      effect_image=excluded.effect_image, availability=excluded.availability,
                      source=excluded.source, parameters_json=excluded.parameters_json,
                      sort_order=excluded.sort_order,
                      availability_reason=excluded.availability_reason,
                      catalog_state=excluded.catalog_state,
                      current_version=excluded.current_version, updated_at=excluded.updated_at""",
                    (
                        spec.id, spec.category, spec.name, spec.function_name, description,
                        details["effect_image"], details["availability"], spec.source,
                        _json(details["parameters"]), order, details["availability_reason"],
                        details["status"], spec.version, _now(), _now(),
                    ),
                )
                schema = manifest(spec)
                db.execute(
                    """INSERT OR IGNORE INTO tool_versions
                    (id, tool_id, version, implementation_key, params_schema_json,
                     inputs_schema_json, outputs_schema_json, code_hash, dependency_json,
                     published_at, state)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, 'published')""",
                    (
                        version_id, spec.id, spec.version, spec.function_name,
                        _json(schema["params_schema"]), _json(schema["inputs_schema"]),
                        _json(schema["outputs_schema"]), code_hash, _now(),
                    ),
                )
                db.execute(
                    """INSERT OR IGNORE INTO tool_examples
                    (id, tool_version_id, title, input_refs_json, params_json,
                     roi_json, result_refs_json, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                    (
                        f"{version_id}:demo", version_id, "内置合成图示例",
                        _json(example_inputs),
                        _json(details["parameters"]),
                        _json([18, 20, 142, 65]) if spec.needs_roi else None,
                        _json(example_results),
                    ),
                )
                if details["status"] == "ready":
                    db.execute(
                        """UPDATE tool_examples SET input_refs_json=?, result_refs_json=?
                        WHERE id=?""",
                        (_json(example_inputs), _json(example_results), f"{version_id}:demo"),
                    )
            for order, row in enumerate(tool_catalog.PENDING_TOOLS, start=len(OPERATORS)):
                tool_id, category, name, function_name, description, _, source, params = row
                state = (
                    "pending_adapter"
                    if tool_id in {"template_match", "ocr", "screen_diff"}
                    else "planned"
                )
                if tool_id == "batch_process":
                    state = "workflow_planned"
                db.execute(
                    """INSERT INTO image_tools
                    (id, category, name, function_name, description, effect_image, availability,
                     source, parameters_json, sort_order, availability_reason, catalog_state,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      category=excluded.category, name=excluded.name,
                      function_name=excluded.function_name, description=excluded.description,
                      availability=excluded.availability, source=excluded.source,
                      parameters_json=excluded.parameters_json, sort_order=excluded.sort_order,
                      availability_reason=excluded.availability_reason,
                      catalog_state=excluded.catalog_state, updated_at=excluded.updated_at""",
                    (
                        tool_id, category, name, function_name, description,
                        "device_only" if state == "pending_adapter" else "planned", source,
                        _json(params), order,
                        "图片适配器待接入" if state == "pending_adapter" else "规划中",
                        state, _now(), _now(),
                    ),
                )

    def _materialize_demo(
        self, db: sqlite3.Connection, spec: OperatorSpec
    ) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
        """Persist a deterministic example for this exact published version."""
        from pixelforge.geometry.mapper import Rect
        from pixelforge.image_lab.operators import run_operator

        source = tool_catalog.demo_image()
        roi = Rect(18, 20, 142, 65) if spec.needs_roi else None
        result = run_operator(spec.id, source, roi=roi)
        source_hash = hashlib.sha256(source.tobytes()).hexdigest()
        source_id = self._example_asset(db, f"builtin-demo-source:{source_hash}", source)
        inputs: dict[str, dict[str, str]] = {"image": {"asset_id": source_id}}
        outputs: dict[str, dict[str, str]] = {}
        for port, pixels in result.images.items():
            asset_id = self._example_asset(db, f"{spec.id}@{spec.version}:{port}", pixels)
            outputs[port] = {
                "asset_id": asset_id,
                "url": f"/api/image-lab/assets/{asset_id}/content",
            }
        return inputs, outputs

    def _example_asset(self, db: sqlite3.Connection, key: str, image: np.ndarray) -> str:
        asset_id = uuid.uuid5(uuid.NAMESPACE_URL, f"pixelforge-example:{key}").hex
        encoded_image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR) if image.ndim == 3 else image
        ok, encoded = cv2.imencode(".png", encoded_image)
        if not ok:
            raise ValueError(f"could not encode example {key}")
        payload = bytes(encoded)
        digest = hashlib.sha256(payload).hexdigest()
        relative = Path("examples") / asset_id[:2] / f"{asset_id}.png"
        path = self.files / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"published example changed without a new version: {key}")
        else:
            temp = path.with_suffix(".tmp")
            temp.write_bytes(payload)
            os.replace(temp, path)
        db.execute(
            """INSERT OR IGNORE INTO image_assets
            (id, kind, origin, import_batch_id, relative_name, storage_path, preview_path,
             sha256, mime_type, byte_size, width, height, channels, orientation, created_at)
            VALUES (?, 'example', 'synthetic', NULL, ?, ?, ?, ?, 'image/png', ?, ?, ?, ?,
                    'generated', ?)""",
            (
                asset_id, key, str(relative), str(relative), digest, len(payload),
                int(image.shape[1]), int(image.shape[0]),
                "MASK8" if image.ndim == 2 else "RGB8", _now(),
            ),
        )
        return asset_id

    def list_tools(self) -> list[dict[str, object]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM image_tools ORDER BY sort_order, id").fetchall()
            counts = dict(db.execute(
                "SELECT tool_id, COUNT(*) FROM tool_versions GROUP BY tool_id"
            ).fetchall())
        items: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["parameters"] = json.loads(str(item["parameters_json"]))
            spec = OPERATORS.get(str(item["id"]))
            item["params_schema"] = [param.as_dict() for param in spec.params] if spec else []
            item["outputs"] = dict(spec.outputs) if spec else {}
            item["version_count"] = counts.get(item["id"], 0)
            items.append(item)
        return items

    def list_examples(self, tool_id: str) -> list[dict[str, object]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                """SELECT e.* FROM tool_examples e JOIN tool_versions v
                ON v.id=e.tool_version_id WHERE v.tool_id=? ORDER BY e.sort_order, e.id""",
                (tool_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "input_refs": json.loads(row["input_refs_json"]),
                "params": json.loads(row["params_json"]),
                "roi": json.loads(row["roi_json"]) if row["roi_json"] else None,
                "result_refs": json.loads(row["result_refs_json"]),
            }
            for row in rows
        ]

    def import_image(
        self, payload: bytes, *, relative_name: str, batch_id: str | None = None,
        origin: str = "local_import",
    ) -> dict[str, object]:
        # One server worker is an application invariant. Serialize imports so
        # the per-batch count/byte checks cannot race within that worker.
        with self._import_lock:
            return self._import_image(
                payload, relative_name=relative_name, batch_id=batch_id, origin=origin
            )

    def _import_image(
        self, payload: bytes, *, relative_name: str, batch_id: str | None,
        origin: str,
    ) -> dict[str, object]:
        name = _relative_name(relative_name)
        if not payload or len(payload) > MAX_IMAGE_BYTES:
            raise ValueError("image must be between 1 byte and 20 MB")
        if batch_id is not None and (len(batch_id) > 100 or not batch_id.isascii()):
            raise ValueError("invalid import batch id")
        if batch_id is not None:
            with closing(self.connect()) as db:
                row = db.execute(
                    """SELECT COUNT(*) AS files, COALESCE(SUM(byte_size), 0) AS bytes
                    FROM image_assets WHERE import_batch_id=?""",
                    (batch_id,),
                ).fetchone()
                if row["files"] >= MAX_BATCH_FILES or row["bytes"] + len(payload) > MAX_BATCH_BYTES:
                    raise ValueError("import batch exceeds 500 images or 512 MB")
        encoded = np.frombuffer(payload, np.uint8)
        try:
            original = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
        except cv2.error as exc:
            raise ValueError("unsupported image") from exc
        if original is None:
            raise ValueError("unsupported image")
        if original.shape[0] * original.shape[1] > MAX_PIXELS:
            raise ValueError("image exceeds 30 megapixels")
        if original.ndim == 3 and original.shape[2] == 4:
            alpha = original[:, :, 3:4].astype(np.float32) / 255.0
            bgr = np.clip(original[:, :, :3] * alpha + 255 * (1 - alpha), 0, 255).astype(np.uint8)
            orientation = "alpha composited on white"
        else:
            try:
                bgr = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            except cv2.error as exc:
                raise ValueError("unsupported image") from exc
            if bgr is None:
                raise ValueError("unsupported image")
            orientation = "EXIF applied by OpenCV decoder"
        ok, preview = cv2.imencode(".png", bgr)
        if not ok:
            raise ValueError("could not normalize image")
        scale = min(1.0, 112 / max(bgr.shape[0], bgr.shape[1]))
        thumb_pixels = cv2.resize(
            bgr,
            (max(1, round(bgr.shape[1] * scale)), max(1, round(bgr.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
        ok, thumbnail = cv2.imencode(".png", thumb_pixels)
        if not ok:
            raise ValueError("could not generate thumbnail")
        asset_id = uuid.uuid4().hex
        subdir = Path("assets") / asset_id[:2] / asset_id
        directory = self.files / subdir
        directory.mkdir(parents=True, exist_ok=False)
        source_path = directory / "source.bin"
        preview_path = directory / "preview.png"
        thumbnail_path = directory / "thumbnail.png"
        try:
            for target, data in (
                (source_path, payload), (preview_path, bytes(preview)),
                (thumbnail_path, bytes(thumbnail)),
            ):
                temp = target.with_suffix(target.suffix + ".tmp")
                temp.write_bytes(data)
                os.replace(temp, target)
            item: dict[str, object] = {
                "id": asset_id,
                "kind": "original",
                "origin": origin,
                "import_batch_id": batch_id,
                "relative_name": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "mime_type": "application/octet-stream",
                "byte_size": len(payload),
                "width": int(bgr.shape[1]),
                "height": int(bgr.shape[0]),
                "channels": "RGB8",
                "orientation": orientation,
                "created_at": _now(),
                "preview_url": f"/api/image-lab/assets/{asset_id}/content",
                "thumbnail_url": f"/api/image-lab/assets/{asset_id}/content?variant=thumbnail",
            }
            with closing(self.connect()) as db, db:
                db.execute(
                    """INSERT INTO image_assets
                    (id, kind, origin, import_batch_id, relative_name, storage_path, preview_path,
                     sha256, mime_type, byte_size, width, height, channels, orientation, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        asset_id, "original", origin, batch_id, name,
                        str(subdir / "source.bin"), str(subdir / "preview.png"),
                        item["sha256"], "application/octet-stream", len(payload), item["width"],
                        item["height"], "RGB8", orientation, item["created_at"],
                    ),
                )
            return item
        except BaseException:
            for path in directory.iterdir():
                path.unlink()
            directory.rmdir()
            raise

    def list_assets(
        self, *, batch_id: str | None = None, limit: int = 100
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with closing(self.connect()) as db:
            if batch_id is None:
                rows = db.execute(
                    """SELECT * FROM image_assets WHERE deleted_at IS NULL AND kind='original'
                    ORDER BY created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT * FROM image_assets WHERE deleted_at IS NULL AND kind='original'
                    AND import_batch_id=?
                    ORDER BY relative_name, id LIMIT ?""",
                    (batch_id, limit),
                ).fetchall()
        return [self._public_asset(row) for row in rows]

    def get_asset(self, asset_id: str) -> dict[str, object] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM image_assets WHERE id=? AND deleted_at IS NULL", (asset_id,)
            ).fetchone()
        return self._public_asset(row) if row else None

    @staticmethod
    def _public_asset(row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        item.pop("storage_path")
        item.pop("preview_path")
        item.pop("deleted_at")
        item["preview_url"] = f"/api/image-lab/assets/{item['id']}/content"
        item["thumbnail_url"] = (
            f"/api/image-lab/assets/{item['id']}/content?variant=thumbnail"
        )
        return item

    def content_path(self, asset_id: str, *, variant: str = "preview") -> Path | None:
        if len(asset_id) != 32 or any(c not in "0123456789abcdef" for c in asset_id):
            return None
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT storage_path, preview_path FROM image_assets
                WHERE id=? AND deleted_at IS NULL""",
                (asset_id,),
            ).fetchone()
        if row is None:
            return None
        relative = Path(row["storage_path"] if variant == "original" else row["preview_path"])
        if variant == "thumbnail":
            relative = relative.with_name("thumbnail.png")
        path = (self.files / relative).resolve()
        if not path.is_relative_to(self.files.resolve()) or not path.is_file():
            return None
        return path

"""명세 버전 저장 (R-1.11: 확정은 사용자 클릭으로만, 버전 증가·이전 버전 보관)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict

from tongdory2.db import new_id, now_iso
from tongdory2.spec.extract_standard import Extraction

SCHEMA = """
CREATE TABLE IF NOT EXISTS spec_versions(
  spec_id TEXT PRIMARY KEY,
  version INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('CONFIRMED','SUPERSEDED')),
  active_profile TEXT NOT NULL,
  current_root_id TEXT,
  body TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);
"""


class SpecStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.executescript(SCHEMA)

    def confirm(self, ex: Extraction, adopted_ids: set[str], profile: str, current_root_id: str | None,
                *, collect_only: bool = False, structure_reference: str = "") -> dict:
        nodes = []
        for n in ex.nodes:
            d = asdict(n)
            d["embedding_text"] = n.embedding_text
            if n.node_id in adopted_ids:
                d["status"] = "ADOPTED"
                if n.status == "CANDIDATE":
                    d["origin"] = "USER"
            elif n.status == "ADOPTED":
                d["status"] = "REJECTED"
            nodes.append(d)
        body = {
            "collect_only": collect_only,
            "structure_reference": structure_reference,
            "projects": ex.projects,
            "profiles": ex.profiles,
            "active_profile": profile,
            "nodes": nodes,
            "variants": ex.variants,
            "entries": ex.entries,
            "std_unmatched": ex.std_unmatched,
            "naming": {"top_style": "NN name", "child_style": "NN.name", "number_width": 2},
        }
        canon = json.dumps(
            sorted(((d["node_id"], d["status"], d["display_path"]) for d in nodes)), ensure_ascii=False
        )
        h = hashlib.sha256(canon.encode()).hexdigest()
        with self.conn:
            row = self.conn.execute("SELECT MAX(version) FROM spec_versions").fetchone()
            version = (row[0] or 0) + 1
            self.conn.execute("UPDATE spec_versions SET status='SUPERSEDED' WHERE status='CONFIRMED'")
            sid = new_id("spec")
            self.conn.execute(
                "INSERT INTO spec_versions VALUES(?,?,?,?,?,?,?,?)",
                (sid, version, "CONFIRMED", profile, current_root_id,
                 json.dumps(body, ensure_ascii=False), h, now_iso()),
            )
        return {"spec_id": sid, "version": version, "content_hash": h}

    def current(self, *, include_previous: bool = False) -> dict | None:
        condition = "" if include_previous else "WHERE status='CONFIRMED' "
        r = self.conn.execute(
            "SELECT spec_id, version, active_profile, body, content_hash, confirmed_at FROM spec_versions "
            + condition + "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not r:
            return None
        return {
            "spec_id": r[0], "version": r[1], "active_profile": r[2], "body": json.loads(r[3]),
            "content_hash": r[4], "confirmed_at": r[5],
        }

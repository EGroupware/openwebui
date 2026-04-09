from functools import lru_cache
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

OPENAPI_DIR = Path("/app/openapi")
INDEX_FILE = OPENAPI_DIR / "egroupware_index_openapi.json"

EGW_BASE_URL = os.getenv("EGW_BASE_URL", "").rstrip("/")
EGW_SERVICE_USERNAME = os.getenv("EGW_SERVICE_USERNAME", "")
EGW_SERVICE_PASSWORD = os.getenv("EGW_SERVICE_PASSWORD", "")

app = FastAPI(
    title="EGroupware OpenAPI Tool Server",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/openapi", StaticFiles(directory=OPENAPI_DIR), name="openapi")


def _resolve_ref(ref: str, base_dir: Path) -> dict:
    """Resolve a $ref like './file.json#/paths/~1foo~1' into the referenced dict."""
    if "#" in ref:
        file_part, pointer = ref.split("#", 1)
    else:
        file_part, pointer = ref, ""

    target_file = (base_dir / file_part).resolve()
    with target_file.open("r", encoding="utf-8") as f:
        doc = json.load(f)

    if pointer:
        # JSON Pointer: decode ~1 -> / and ~0 -> ~
        for token in pointer.strip("/").split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            doc = doc[token]

    return doc


@lru_cache(maxsize=1)
def build_merged_openapi() -> str:
    """Build a fully merged (dereferenced) OpenAPI spec from the index + individual files."""
    if not INDEX_FILE.is_file():
        raise FileNotFoundError(f"Missing OpenAPI index file: {INDEX_FILE}")

    with INDEX_FILE.open("r", encoding="utf-8") as f:
        index = json.load(f)

    merged_paths: dict = {}
    merged_components: dict = {"securitySchemes": {}, "schemas": {}}

    for path, path_item in index.get("paths", {}).items():
        if "$ref" in path_item:
            resolved = _resolve_ref(path_item["$ref"], OPENAPI_DIR)
            merged_paths[path] = resolved
            # Also pull components from the same source file
            ref_str: str = path_item["$ref"]
            file_part = ref_str.split("#")[0]
            src_file = (OPENAPI_DIR / file_part).resolve()
            with src_file.open("r", encoding="utf-8") as f:
                src_doc = json.load(f)
            for section, items in src_doc.get("components", {}).items():
                if section not in merged_components:
                    merged_components[section] = {}
                merged_components[section].update(items)
        else:
            merged_paths[path] = path_item

    servers = index.get("servers", [])
    if EGW_BASE_URL:
        servers = [{"url": EGW_BASE_URL, "description": "EGroupware instance"}]

    merged: dict = {
        "openapi": index.get("openapi", "3.1.0"),
        "info": index.get("info", {}),
        "servers": servers,
        "security": index.get("security", []),
        "paths": merged_paths,
        "components": merged_components,
    }

    return json.dumps(merged)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/openapi.json")
async def get_openapi() -> dict:
    try:
        return json.loads(build_merged_openapi())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except (json.JSONDecodeError, KeyError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build merged OpenAPI spec: {exc}",
        ) from exc

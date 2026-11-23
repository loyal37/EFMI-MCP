"""Build one portable Python script with a native, versioned fingerprint catalog."""
import argparse
import base64
import json
from pathlib import Path
import zlib
from efmi_texture_repair import make_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "dist/texture-repair/efmi_texture_repair.py")
    args = parser.parse_args()
    table = json.loads(args.table.read_text(encoding="utf-8"))
    catalog = make_catalog(table)
    data = base64.b85encode(zlib.compress(json.dumps(catalog, separators=(",", ":")).encode(), 9)).decode()
    source = Path(__file__).with_name("efmi_texture_repair.py").read_text(encoding="utf-8")
    assert source.count('CATALOG_B85 = ""') == 1
    source = source.replace('CATALOG_B85 = ""', "CATALOG_B85 = " + repr(data))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(source, encoding="utf-8")
    print(json.dumps(dict(output=str(args.output), game_version=catalog["game_version"],
                          known_meshes=len(catalog["meshes"]), eligible_meshes=sum(bool(v) for v in catalog["meshes"].values()),
                          roles=sum(map(len, catalog["meshes"].values())),
                          bytes=args.output.stat().st_size)))


if __name__ == "__main__":
    main()

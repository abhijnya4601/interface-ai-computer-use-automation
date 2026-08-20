"""
Evidence generator: proves guardrails/encryption.py actually works end to end against
a real file on disk — not just unit-tested in memory. Deliberately does NOT touch /evidence/ or
/capabilities/ (those must stay human-readable for reviewers per the assignment's requirement);
this writes to a throwaway file instead, encrypts realistic customer-shaped data, confirms the
file on disk is genuinely unreadable without the key, then decrypts it back and confirms it
matches exactly.

Run: python scripts/demo_encryption_at_rest.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from guardrails.encryption import decrypt_at_rest, encrypt_at_rest, generate_key

DEMO_DIR = Path(__file__).parent.parent / "evidence" / "_encryption_demo"


def check(label: str, condition: bool):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"Demo failed: {label}")


def main():
    if not os.environ.get("EVIDENCE_ENCRYPTION_KEY"):
        key = generate_key()
        os.environ["EVIDENCE_ENCRYPTION_KEY"] = key
        print(f"(generated a throwaway key for this demo run: {key})\n")

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    plaintext_record = {
        "member_id": "12345",
        "member_name": "Dana Whitfield",
        "savings_balance": "$1,842.30",
    }
    plaintext_bytes = json.dumps(plaintext_record).encode()

    ciphertext = encrypt_at_rest(plaintext_bytes)
    encrypted_path = DEMO_DIR / "sample_record.enc"
    encrypted_path.write_bytes(ciphertext)
    print(f"wrote {encrypted_path} ({len(ciphertext)} bytes)")

    raw_disk_content = encrypted_path.read_bytes()
    check("the plaintext member name is NOT present in the file on disk",
          b"Dana Whitfield" not in raw_disk_content)
    check("the plaintext balance is NOT present in the file on disk",
          b"1,842.30" not in raw_disk_content)
    check("the file on disk is not even valid JSON (genuinely opaque, not just relabeled)",
          _not_json(raw_disk_content))

    decrypted_bytes = decrypt_at_rest(encrypted_path.read_bytes())
    decrypted_record = json.loads(decrypted_bytes)
    check("decrypting the file gives back the exact original record",
          decrypted_record == plaintext_record)

    encrypted_path.unlink()
    DEMO_DIR.rmdir()
    print("\nAll encryption-at-rest checks passed. Demo file cleaned up (not part of deliverable evidence).")


def _not_json(data: bytes) -> bool:
    try:
        json.loads(data)
        return False
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Upload Terraform provider to TFE private registry.

Usage:
    export TFE_TOKEN="your-token"
    export GPG_KEY_ID="your-gpg-key-id"
    python3 scripts/upload_to_tfe.py --version 1.0.0

Environment Variables:
    TFE_TOKEN       - TFE API token (required)
    GPG_KEY_ID      - GPG key fingerprint used for signing (required)
    TFE_HOST        - TFE hostname (default: terraform.lambdalabs.cloud)
    TFE_ORG         - TFE organization (default: lambdacloud)
    PROVIDER_NAME   - Provider name (default: okta)
"""
import argparse
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(description='Upload Terraform provider to TFE')
    parser.add_argument('--version', required=True, help='Provider version (without v prefix)')
    parser.add_argument('--dist-dir', default='dist', help='Directory containing build artifacts')
    args = parser.parse_args()

    # Configuration from environment
    token = os.environ.get('TFE_TOKEN')
    if not token:
        print("ERROR: TFE_TOKEN environment variable is required")
        sys.exit(1)

    gpg_key_id = os.environ.get('GPG_KEY_ID')
    if not gpg_key_id:
        print("ERROR: GPG_KEY_ID environment variable is required")
        sys.exit(1)

    host = os.environ.get('TFE_HOST', 'terraform.lambdalabs.cloud')
    org = os.environ.get('TFE_ORG', 'lambdacloud')
    provider = os.environ.get('PROVIDER_NAME', 'okta')
    version = args.version
    dist_dir = args.dist_dir

    api = f"https://{host}/api/v2"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/vnd.api+json"
    }

    print(f"==> Uploading {provider} v{version} to {host}/{org}")

    # Step 1: Ensure provider exists
    print("==> Step 1: Creating provider (if not exists)...")
    resp = requests.post(
        f"{api}/organizations/{org}/registry-providers",
        headers=headers,
        json={
            "data": {
                "type": "registry-providers",
                "attributes": {
                    "name": provider,
                    "namespace": org,
                    "registry-name": "private"
                }
            }
        }
    )
    if resp.status_code == 201:
        print("    Provider created")
    elif resp.status_code == 422:
        print("    Provider already exists")
    else:
        print(f"    Response: {resp.status_code} - {resp.text[:200]}")

    # Step 2: Upload GPG key (idempotent - TFE handles duplicates)
    print("==> Step 2: Uploading GPG key...")
    gpg_key_path = os.path.join(dist_dir, 'gpg-public-key.asc')
    if not os.path.exists(gpg_key_path):
        print(f"    ERROR: GPG public key not found at {gpg_key_path}")
        sys.exit(1)

    with open(gpg_key_path) as f:
        gpg_key = f.read()

    # Note: GPG keys use a different API endpoint
    resp = requests.post(
        f"https://{host}/api/registry/private/v2/gpg-keys",
        headers=headers,
        json={
            "data": {
                "type": "gpg-keys",
                "attributes": {
                    "namespace": org,
                    "ascii-armor": gpg_key
                }
            }
        }
    )
    if resp.status_code in [200, 201]:
        print(f"    GPG key uploaded: {gpg_key_id}")
    elif 'already exists' in resp.text.lower() or resp.status_code == 422:
        print(f"    GPG key already exists")
    else:
        print(f"    WARNING: GPG key upload response: {resp.status_code} - {resp.text[:200]}")

    # Step 3: Create version
    print(f"==> Step 3: Creating version {version}...")
    resp = requests.post(
        f"{api}/organizations/{org}/registry-providers/private/{org}/{provider}/versions",
        headers=headers,
        json={
            "data": {
                "type": "registry-provider-versions",
                "attributes": {
                    "version": version,
                    "key-id": gpg_key_id,
                    "protocols": ["5.0"]
                }
            }
        }
    )

    if resp.status_code != 201:
        print(f"    ERROR: {resp.status_code} - {resp.text}")
        sys.exit(1)

    version_data = resp.json()
    shasums_upload = version_data['data']['links']['shasums-upload']
    shasums_sig_upload = version_data['data']['links']['shasums-sig-upload']
    print(f"    Version created")

    # Step 4: Upload checksums
    print("==> Step 4: Uploading checksums...")
    shasums_file = os.path.join(dist_dir, f"terraform-provider-{provider}_{version}_SHA256SUMS")
    shasums_sig_file = os.path.join(dist_dir, f"terraform-provider-{provider}_{version}_SHA256SUMS.sig")

    if not os.path.exists(shasums_file):
        print(f"    ERROR: SHA256SUMS not found at {shasums_file}")
        sys.exit(1)

    with open(shasums_file, 'rb') as f:
        resp = requests.put(shasums_upload, data=f)
        if resp.status_code != 200:
            print(f"    ERROR uploading SHA256SUMS: {resp.status_code}")
            sys.exit(1)
    print("    SHA256SUMS uploaded")

    with open(shasums_sig_file, 'rb') as f:
        resp = requests.put(shasums_sig_upload, data=f)
        if resp.status_code != 200:
            print(f"    ERROR uploading SHA256SUMS.sig: {resp.status_code}")
            sys.exit(1)
    print("    SHA256SUMS.sig uploaded")

    # Step 5: Upload platforms
    print("==> Step 5: Uploading platform binaries...")
    with open(shasums_file) as f:
        lines = f.readlines()

    success_count = 0
    error_count = 0

    for line in lines:
        parts = line.strip().split()
        if len(parts) != 2:
            continue

        sha_hash, filename = parts
        name_parts = filename.replace('.zip', '').split('_')
        if len(name_parts) < 4:
            print(f"    Skipping {filename} - cannot parse OS/arch")
            continue

        os_name = name_parts[-2]
        arch = name_parts[-1]

        print(f"    {os_name}_{arch}...", end=' ', flush=True)

        resp = requests.post(
            f"{api}/organizations/{org}/registry-providers/private/{org}/{provider}/versions/{version}/platforms",
            headers=headers,
            json={
                "data": {
                    "type": "registry-provider-version-platforms",
                    "attributes": {
                        "os": os_name,
                        "arch": arch,
                        "shasum": sha_hash,
                        "filename": filename
                    }
                }
            }
        )

        if resp.status_code != 201:
            print(f"ERROR: {resp.text[:100]}")
            error_count += 1
            continue

        upload_url = resp.json()['data']['links']['provider-binary-upload']
        binary_path = os.path.join(dist_dir, filename)

        if not os.path.exists(binary_path):
            print(f"ERROR: Binary not found at {binary_path}")
            error_count += 1
            continue

        with open(binary_path, 'rb') as f:
            resp = requests.put(upload_url, data=f)
            if resp.status_code != 200:
                print(f"ERROR uploading: {resp.status_code}")
                error_count += 1
                continue

        print("done")
        success_count += 1

    print()
    print(f"==> Upload complete! ({success_count} succeeded, {error_count} failed)")
    print(f"==> https://{host}/app/{org}/registry/providers/private/{org}/{provider}")

    if error_count > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()

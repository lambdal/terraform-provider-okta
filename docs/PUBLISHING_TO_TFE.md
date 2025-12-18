# Publishing terraform-provider-okta to Terraform Enterprise

This document describes how to publish the Okta Terraform provider to our private Terraform Enterprise registry at `terraform.lambdalabs.cloud`.

## Overview

Publishing a provider to TFE requires:
1. Building binaries for all supported platforms
2. Creating checksums and GPG signatures
3. Uploading the GPG public key to TFE (one-time setup)
4. Creating a provider and version in the registry
5. Uploading all artifacts via the TFE API

## Prerequisites

### Tools Required
- **Go** (1.21+)
- **GoReleaser** (v2): `go install github.com/goreleaser/goreleaser/v2@latest`
- **GPG**: For signing releases
- **Python 3**: For the upload script (with `requests` library)
- **curl** and **jq**: For API interactions

### GPG Key Setup

Create a GPG key without a passphrase for CI/CD use:

```bash
cat > /tmp/gpg-batch <<EOF
%no-protection
Key-Type: RSA
Key-Length: 4096
Subkey-Type: RSA
Subkey-Length: 4096
Name-Real: Terraform Provider Release
Name-Email: release@lambdal.com
Expire-Date: 0
%commit
EOF

gpg --batch --generate-key /tmp/gpg-batch
```

Note the key ID from the output (e.g., `70A2B15ADEB2EEDD`).

Export the private key for CI/CD:
```bash
gpg --armor --export-secret-keys YOUR_KEY_ID > private-key.asc
```

### TFE API Token

Generate a TFE API token with "Manage Private Registry" permissions from:
`https://terraform.lambdalabs.cloud/app/settings/tokens`

## Manual Publishing Process

### Step 1: Create Git Tag

```bash
git tag v1.0.0
```

### Step 2: Build with GoReleaser

```bash
export GPG_FINGERPRINT="YOUR_GPG_KEY_ID"
goreleaser release --clean --skip=publish --parallelism=1
```

This creates in `dist/`:
- `terraform-provider-okta_VERSION_OS_ARCH.zip` (14 platform binaries)
- `terraform-provider-okta_VERSION_SHA256SUMS`
- `terraform-provider-okta_VERSION_SHA256SUMS.sig`

### Step 3: Export GPG Public Key

```bash
gpg --armor --export $GPG_FINGERPRINT > dist/gpg-public-key.asc
```

### Step 4: Upload to TFE

Run the upload script:
```bash
python3 dist/upload_platforms.py
```

Or manually via API (see detailed steps below).

## TFE API Upload Steps

### 1. Create Provider (one-time)

```bash
curl -s --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/vnd.api+json" \
  --request POST \
  --data '{"data":{"type":"registry-providers","attributes":{"name":"okta","namespace":"lambdacloud","registry-name":"private"}}}' \
  "https://terraform.lambdalabs.cloud/api/v2/organizations/lambdacloud/registry-providers"
```

### 2. Upload GPG Key (one-time per key)

```bash
# Create payload
python3 -c "
import json
with open('dist/gpg-public-key.asc', 'r') as f:
    key = f.read()
payload = {'data': {'type': 'gpg-keys', 'attributes': {'namespace': 'lambdacloud', 'ascii-armor': key}}}
with open('gpg-key-payload.json', 'w') as f:
    json.dump(payload, f)
"

curl -s --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/vnd.api+json" \
  --request POST \
  --data @gpg-key-payload.json \
  "https://terraform.lambdalabs.cloud/api/registry/private/v2/gpg-keys"
```

### 3. Create Provider Version

```bash
curl -s --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/vnd.api+json" \
  --request POST \
  --data '{"data":{"type":"registry-provider-versions","attributes":{"version":"1.0.0","key-id":"YOUR_GPG_KEY_ID","protocols":["5.0"]}}}' \
  "https://terraform.lambdalabs.cloud/api/v2/organizations/lambdacloud/registry-providers/private/lambdacloud/okta/versions"
```

This returns upload URLs for `shasums-upload` and `shasums-sig-upload`.

### 4. Upload Checksums

```bash
curl -s --request PUT --upload-file "dist/terraform-provider-okta_1.0.0_SHA256SUMS" "$SHASUMS_UPLOAD_URL"
curl -s --request PUT --upload-file "dist/terraform-provider-okta_1.0.0_SHA256SUMS.sig" "$SHASUMS_SIG_UPLOAD_URL"
```

### 5. Create Platforms and Upload Binaries

For each platform (linux_amd64, darwin_arm64, etc.):

```bash
# Create platform
curl -s --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/vnd.api+json" \
  --request POST \
  --data '{"data":{"type":"registry-provider-version-platforms","attributes":{"os":"linux","arch":"amd64","shasum":"HASH_FROM_SHA256SUMS","filename":"terraform-provider-okta_1.0.0_linux_amd64.zip"}}}' \
  "https://terraform.lambdalabs.cloud/api/v2/organizations/lambdacloud/registry-providers/private/lambdacloud/okta/versions/1.0.0/platforms"

# Upload binary using the returned provider-binary-upload URL
curl -s --request PUT --upload-file "dist/terraform-provider-okta_1.0.0_linux_amd64.zip" "$BINARY_UPLOAD_URL"
```

## GitHub Actions Workflow

Create `.github/workflows/release.yml`:

```yaml
name: Release to TFE

on:
  push:
    tags:
      - 'v*'

permissions:
  contents: read

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Go
        uses: actions/setup-go@v5
        with:
          go-version-file: 'go.mod'

      - name: Import GPG key
        id: import_gpg
        uses: crazy-max/ghaction-import-gpg@v6
        with:
          gpg_private_key: ${{ secrets.GPG_PRIVATE_KEY }}

      - name: Run GoReleaser
        uses: goreleaser/goreleaser-action@v6
        with:
          version: '~> v2'
          args: release --clean --skip=publish --parallelism=2
        env:
          GPG_FINGERPRINT: ${{ steps.import_gpg.outputs.fingerprint }}

      - name: Export GPG public key
        run: |
          gpg --armor --export ${{ steps.import_gpg.outputs.fingerprint }} > dist/gpg-public-key.asc

      - name: Upload to TFE
        env:
          TFE_TOKEN: ${{ secrets.TFE_TOKEN }}
          TFE_HOST: terraform.lambdalabs.cloud
          TFE_ORG: lambdacloud
          PROVIDER_NAME: okta
          GPG_KEY_ID: ${{ steps.import_gpg.outputs.fingerprint }}
        run: |
          # Extract version from tag
          VERSION=${GITHUB_REF#refs/tags/v}

          # Install requests
          pip install requests

          # Run upload script
          python3 scripts/upload_to_tfe.py --version "$VERSION"
```

### Upload Script for CI

Create `scripts/upload_to_tfe.py`:

```python
#!/usr/bin/env python3
"""Upload Terraform provider to TFE private registry."""
import argparse
import json
import os
import sys
import requests

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True, help='Provider version (without v prefix)')
    args = parser.parse_args()

    # Configuration from environment
    token = os.environ['TFE_TOKEN']
    host = os.environ.get('TFE_HOST', 'terraform.lambdalabs.cloud')
    org = os.environ.get('TFE_ORG', 'lambdacloud')
    provider = os.environ.get('PROVIDER_NAME', 'okta')
    gpg_key_id = os.environ['GPG_KEY_ID']
    version = args.version
    dist_dir = 'dist'

    api = f"https://{host}/api/v2"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/vnd.api+json"
    }

    print(f"==> Uploading {provider} v{version} to {host}/{org}")

    # Step 1: Ensure provider exists
    print("==> Creating provider (if not exists)...")
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
        print(f"    Response: {resp.status_code}")

    # Step 2: Upload GPG key (idempotent - TFE handles duplicates)
    print("==> Uploading GPG key...")
    gpg_key_path = os.path.join(dist_dir, 'gpg-public-key.asc')
    with open(gpg_key_path) as f:
        gpg_key = f.read()

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
    else:
        print(f"    GPG key response: {resp.status_code} - {resp.text[:200]}")

    # Step 3: Create version
    print(f"==> Creating version {version}...")
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
    print("==> Uploading checksums...")
    shasums_file = os.path.join(dist_dir, f"terraform-provider-{provider}_{version}_SHA256SUMS")
    shasums_sig_file = os.path.join(dist_dir, f"terraform-provider-{provider}_{version}_SHA256SUMS.sig")

    with open(shasums_file, 'rb') as f:
        requests.put(shasums_upload, data=f)
    print("    SHA256SUMS uploaded")

    with open(shasums_sig_file, 'rb') as f:
        requests.put(shasums_sig_upload, data=f)
    print("    SHA256SUMS.sig uploaded")

    # Step 5: Upload platforms
    print("==> Uploading platform binaries...")
    with open(shasums_file) as f:
        lines = f.readlines()

    for line in lines:
        parts = line.strip().split()
        if len(parts) != 2:
            continue

        sha_hash, filename = parts
        name_parts = filename.replace('.zip', '').split('_')
        if len(name_parts) < 4:
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
            continue

        upload_url = resp.json()['data']['links']['provider-binary-upload']
        binary_path = os.path.join(dist_dir, filename)

        with open(binary_path, 'rb') as f:
            requests.put(upload_url, data=f)
        print("done")

    print()
    print(f"==> Upload complete!")
    print(f"==> https://{host}/app/{org}/registry/providers/private/{org}/{provider}")

if __name__ == '__main__':
    main()
```

### Required GitHub Secrets

Configure these secrets in your repository settings:

| Secret | Description |
|--------|-------------|
| `GPG_PRIVATE_KEY` | ASCII-armored GPG private key (output of `gpg --armor --export-secret-keys KEY_ID`) |
| `TFE_TOKEN` | TFE API token with registry management permissions |

### Creating a Release

1. Update version as needed
2. Create and push a tag:
   ```bash
   git tag v1.0.1
   git push origin v1.0.1
   ```
3. GitHub Actions will automatically build and publish to TFE

## Using the Provider

Once published, use the provider in your Terraform configurations:

```hcl
terraform {
  required_providers {
    okta = {
      source  = "terraform.lambdalabs.cloud/lambdacloud/okta"
      version = "~> 1.0"
    }
  }
}

provider "okta" {
  # Configuration options
}
```

## Troubleshooting

### "GPG signing failed"
- Ensure the GPG key has no passphrase for CI/CD use
- Verify `GPG_FINGERPRINT` environment variable is set correctly

### "Provider not found" in Terraform
- Verify the source matches: `terraform.lambdalabs.cloud/lambdacloud/okta`
- Check that all platform binaries were uploaded successfully
- Ensure your Terraform CLI is configured to use the private registry

### API 404 errors
- GPG keys endpoint is at `/api/registry/private/v2/gpg-keys` (not `/api/v2/organizations/.../gpg-keys`)
- Verify organization name is `lambdacloud` (not `lambdalabs`)

### Build resource usage
- Use `--parallelism=1` or `--parallelism=2` to limit concurrent builds
- This prevents system resource exhaustion on smaller machines

## Reference

- [HashiCorp: Publishing Providers](https://developer.hashicorp.com/terraform/registry/providers/publishing)
- [TFE API: Private Registry](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/private-registry/providers)
- [GoReleaser Configuration](https://goreleaser.com/customization/)

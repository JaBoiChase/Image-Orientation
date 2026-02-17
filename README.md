# Image-Orientation Shopify ALT Tagger

This service classifies product image orientation and updates Shopify image ALT text.

## Deploying to Render for a Shopify Custom App

If you are importing this GitHub repo as a **custom app backend** in Shopify and hosting on Render, make sure these are in place:

1. **Web service command**
   - Root directory: `shoe-orientation`
   - Start command: `uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}`
   - If you configure service settings manually in Render, make sure this is a **Web Service** (not Static Site / Background Worker) and that the start command includes a port argument.

2. **Environment variables**
   - `SHOPIFY_SHOP` (for example: `your-store.myshopify.com`)
   - `SHOPIFY_ADMIN_TOKEN` (Admin API access token from your custom app)
   - `SHOPIFY_API_VERSION` (recommended to pin to a currently supported version)
   - `MIN_CONF` (optional, defaults to `0.80`)
   - `RUN_SECRET` (recommended; required for protecting `/api/run` and `/run`)

3. **Admin API scopes in Shopify app**
   - `read_products`
   - `write_files`

4. **Model artifacts must be in repo/deploy image**
   - The service loads vendor-specific models from `shoe-orientation/models/<Vendor>.pt`.
   - Shopify product `vendor` value must match the model filename (after `/` and `\\` are replaced with `-`).

5. **Operational checks**
   - Use `GET /` to verify the UI is reachable.
   - Use `POST /api/run` with `x-run-secret` header if `RUN_SECRET` is set.
   - Validate that the product has `MediaImage` entries in `READY` state.

## API quick start

### JSON endpoint

`POST /api/run`

```json
{
  "product_id": 123456789,
  "min_conf": 0.8
}
```

Optional auth header:

- `x-run-secret: <RUN_SECRET>`

### Form endpoint

- `POST /run` with fields: `product_id`, optional `min_conf`, optional `secret`

## Notes

- Existing ALT text is currently overwritten for images that meet confidence threshold.
- Requests are processed synchronously, so very large products may take longer.


## Render "no open ports detected" troubleshooting

If Render reports that no port is open or no port is specified:

1. Confirm the service type is **Web Service**.
2. Confirm `rootDir` is `shoe-orientation` so Render can find `app.py`.
3. Confirm start command exactly binds host/port:
   - `uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}`
4. If deploying without `render.yaml`, set the same start command in the Render dashboard.
5. Check logs for startup crashes (for example missing env vars); if the app exits early, Render also reports no open port.

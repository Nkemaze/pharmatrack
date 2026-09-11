# PharmaTrack

PharmaTrack is a local pharmacy inventory application. It is built with **Flask**, shown in a desktop window with **pywebview**, and stores its data in an **SQLite** database named `pharmacy.db`.

It also provides a versioned **REST API** at `/api/v1`. The API allows another application, such as a mobile app or website, to read or manage pharmacy data according to the user's role.

## What the application manages

- Products and medicine information, including barcode, dosage form, and prescription or controlled-medicine status.
- Batches of a product, including batch number and expiry date.
- Stock movements: receipt, sale, return, transfer, adjustment, destruction, and loss.
- Low-stock and expiry alerts.
- Loss reports and confirmation that a loss was reported to an authority.
- Pharmacy settings such as name, address, location, and low-stock threshold.

## REST API, not FastAPI

This project uses **Flask** as its Python web framework. The API follows **REST** principles:

- URLs represent resources, for example `/api/v1/products`.
- HTTP methods explain the action: `GET` reads, `POST` creates, `PUT` updates, and `PATCH` makes a small update.
- Responses are JSON, so mobile apps, websites, or other platforms can use them.

FastAPI is another Python framework. It is not used in this project.

## How API authentication works

The desktop app and API use different authentication methods:

- The desktop pages use a browser session after a person signs in.
- External API clients use a JWT access token.

To use a protected API endpoint, an external platform first sends a login request:

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "name": "Account Name",
  "password": "password"
}
```

If the details are correct, the API returns an `access_token`. The client then sends that token with later requests:

```http
Authorization: Bearer <access_token>
```

The token contains the account ID and API role. Each endpoint checks the role before performing its work. Passwords are saved as secure hashes, rather than as plain text. The desktop login also temporarily locks an account after five failed attempts.

The login response also includes a `refresh_token`. When the access token
expires, the mobile app sends the refresh token to `POST /api/v1/auth/refresh`
to receive a new access token. `POST /api/v1/auth/logout` revokes the current
access or refresh token so it cannot be used again.

Set a persistent JWT secret before running the app so tokens remain valid when the application restarts:

```powershell
$env:JWT_SECRET_KEY = python -c "import secrets; print(secrets.token_urlsafe(64))"
python app.py
```

For production, the host must set `PHARMATRACK_ENV=production` and a long,
random `JWT_SECRET_KEY`. The application refuses to start in production without
that secret. Access tokens last 30 minutes and refresh tokens last 30 days by
default; change them with `JWT_ACCESS_TOKEN_MINUTES` and
`JWT_REFRESH_TOKEN_DAYS` if needed.

## Roles and permissions

The API is designed for three roles:

| API role | Who it represents | What it can do |
|---|---|---|
| `pharmacy` | Pharmacy staff | Read and manage products, batches, and stock movements. |
| `admin` | Pharmacy administrator | Perform pharmacy actions and access administrative dashboard and loss reports. |
| `user` | Customer or ordinary external user | Read the safe public product list only. |

The desktop application calls pharmacy staff `pharmacist`. When they log in to the API, that role is converted to `pharmacy`.

The safe public list never includes controlled products or exact stock quantities. The current `GET /api/v1/products` endpoint also allows this safe list without a token.

> Note: the current registration screens create `admin` and `pharmacist` accounts. The `user` API role is supported by the API rules, but a normal user-account creation flow still needs to be added.

## CRUD: who handles what

CRUD means **Create, Read, Update, and Delete**.

| Action | Endpoint | Allowed roles | What it does |
|---|---|---|---|
| Read | `GET /api/v1/products` | Public, user, pharmacy, admin | Lists products. Public/user responses are safe and limited; pharmacy/admin see inventory details. |
| Read | `GET /api/v1/products/<product_id>` | pharmacy, admin | Gets full details for one product. |
| Create | `POST /api/v1/auth/refresh` | Refresh token | Creates a new access token. |
| Update | `POST /api/v1/auth/logout` | Any valid token | Revokes the current token. |
| Create | `POST /api/v1/products` | pharmacy, admin | Creates a product, its first batch, and optional opening stock. |
| Update | `PUT /api/v1/products/<product_id>` | pharmacy, admin | Updates medicine information only. |
| Create | `POST /api/v1/users` | admin | Creates a `pharmacy` or read-only `user` API account. |
| Read | `GET /api/v1/products/<product_id>/batches` | pharmacy, admin | Lists the batches and remaining stock for a product. |
| Create | `POST /api/v1/movements` | pharmacy, admin | Records a receipt, sale, return, transfer, adjustment, destruction, or loss. |
| Read | `GET /api/v1/reports/dashboard` | admin | Returns low-stock, expiry, and recent-movement information. |
| Read | `GET /api/v1/reports/losses` | admin | Returns loss reports and loss summaries. |
| Update | `PATCH /api/v1/reports/losses/<loss_report_id>/reported` | admin | Records that an authority received a loss report. |

There is deliberately no delete API for products or movements. Pharmacy stock history is an audit trail and should not be silently removed.

### Stock changes

Stock is never changed by editing a product directly. Instead, the system adds a stock movement:

- A `receipt` or `return` adds stock.
- A `sale`, `transfer`, `destruction`, or `loss` removes stock.
- An `adjustment` can add or remove stock.

Current stock is calculated by adding all movements for each batch. A `loss` movement automatically creates a linked loss report.

The API validates JSON before saving it: booleans must be real JSON `true` or
`false`, quantities must be positive whole numbers, dates must use
`YYYY-MM-DD`, and unknown fields are rejected. A sale or other stock-out action
is refused if it would make a batch's stock negative.

## Database structure

SQLite stores data locally in `pharmacy.db`. The schema is defined in [`database/schema.sql`](database/schema.sql).

```text
product
  └── product_batch
        └── stock_movement
              └── loss_report (only for a loss)

user
  └── can be recorded as the person who performed or approved a movement
```

| Table | Purpose |
|---|---|
| `product` | Medicine details such as name, barcode, prescription status, and controlled status. |
| `product_batch` | A particular received batch, its batch number, and expiry date. |
| `stock_movement` | Every stock change, with the signed quantity, time, reference, and responsible user where available. |
| `loss_report` | Details of a loss and whether it was reported to an authority. |
| `user` | Account ID, name, role, optional device ID, and password hash. |
| `settings` | Pharmacy settings and low-stock threshold. |

All primary IDs are UUID text values. Foreign keys are enabled for every database connection, which prevents records such as a movement for a batch that does not exist.

## Example API requests

Create a product as a pharmacy staff member or admin:

```http
POST /api/v1/products
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "name": "Vitamin C 500mg",
  "category": "Supplements",
  "dosage_form": "Tablet",
  "batch_number": "VC-001",
  "expiry_date": "2027-08-17",
  "initial_quantity": 100
}
```

Record a sale:

```http
POST /api/v1/movements
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "product_batch_id": "batch-uuid-here",
  "movement_type": "sale",
  "quantity": 2,
  "reference_number": "SALE-1001"
}
```

Read the safe public list:

```http
GET /api/v1/products
```

## Running the project

Python 3.10 or later is recommended.

```powershell
pip install -r requirements.txt
python app.py
```

The database is created automatically when the application starts.

On Linux, running `python app.py` opens a native desktop window when GTK or
Qt bindings are available. If neither backend is installed, it automatically
opens the application in the default browser instead. The Flask server remains
running until you stop it with `Ctrl+C`.

## Automated API tests

The test suite creates a temporary SQLite database, so it never changes your
normal `pharmacy.db` data. Run it with:

```powershell
python -m unittest discover -s tests -v
```

## Preparing the API for other platforms

The API routes exist, but the current application is primarily a local desktop application. Before exposing it to external platforms, the project should:

1. Correct API error responses so they consistently return the intended HTTP status codes.
2. Add an admin-controlled way to create and manage the `user` role.
3. Decide whether the safe public list should remain anonymous or require a `user` token.
4. Deploy the Flask service to a reachable server using HTTPS.
5. Use a permanent strong `JWT_SECRET_KEY` and configure CORS only for trusted client applications.
6. Add API documentation, input validation, rate limiting, token-expiry rules, and audit logging.
7. For multiple pharmacies or cloud hosting, move beyond one local SQLite file to a server database such as PostgreSQL and add pharmacy-level data separation.

## Project structure

```text
app.py                 Flask pages, application startup, and API registration
api/                   Versioned REST API routes and JWT role checks
database/schema.sql    SQLite tables, relationships, and indexes
database/db.py         Database connection, setup, and migration helper
database/queries.py    Shared database queries and business rules
templates/             HTML templates for the desktop application
static/                Stylesheets, scripts, and images
```

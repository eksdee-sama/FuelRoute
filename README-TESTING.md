# FuelRoute API - Testing Guide

## Quick Start

### Prerequisites
- Python 3.8+
- Node.js + npm (for Newman)
- Virtual environment activated

### 1. Install Dependencies

```bash
# Install Python deps
pip install -r requirements.txt

# Install Node deps (Newman for Postman tests)
npm install
```

### 2. Configure Environment

Copy `.env.example` to `.env` (already done, edit if needed):
- `ORS_API_KEY` - OpenRouteService API key (included)
- `SECRET_KEY` - Django secret (dev default provided)
- External API endpoints configured to public services

### 3. Start Server

**Option A: Using Python uvicorn** (Recommended)
```bash
python -m uvicorn fuel_route_api.asgi:application --port 8001 --reload
```

**Option B: Using manage.py runserver**
```bash
python manage.py runserver 8001
```

Server will be available at: `http://127.0.0.1:8001`

### 4. Run Tests

**Option A: Automated (Windows)**
```powershell
.\run-tests.ps1
```
- Starts server
- Runs all Postman tests
- Generates HTML report
- Opens report in browser
- Stops server

**Option B: Manual Newman**
```bash
npx newman run -c FuelRoute.postman_collection.json \
  -e FuelRoute-Env.postman_environment.json \
  -r cli,html \
  -H newman-report.html
```

### Test Endpoints

Postman collection includes:
1. **Long route** - Denver to Las Vegas (3 fuel stops)
2. **Direct trip** - Dallas to Austin (1 cheapest stop)
3. **Coordinates input** - Uses lat/lon (no geocoding)
4. **Invalid locations** - Error handling
5. **Edge cases** - Same start/end, short distances

Each test validates:
- Response format (summary, fuel_stops array)
- Fuel costs and distances
- Response time headers (X-Response-Time, X-Cache)
- HTTP status codes

## Test Reports

Reports generated in:
- `newman-report-YYYY-MM-DD-HH-MM-SS.html` - Full HTML report
- Console output shows pass/fail summary

## Troubleshooting

### Server won't start
- Check port 8001 is free: `netstat -ano | findstr :8001`
- Check Python path and venv activation

### Newman tests fail
- Ensure server is running on `http://127.0.0.1:8001`
- Check `.env` has valid `ORS_API_KEY`
- Try single test: `npx newman run -c FuelRoute.postman_collection.json -f "1. Long route..."`

### Missing node_modules
```bash
npm install
```

## Files

- `FuelRoute.postman_collection.json` - API test suite
- `FuelRoute-Env.postman_environment.json` - Postman environment vars
- `run-tests.ps1` - Windows automated test runner
- `.env` - Local configuration (ORS key, etc.)

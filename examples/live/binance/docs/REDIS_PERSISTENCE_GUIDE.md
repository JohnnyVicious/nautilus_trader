# Redis Persistence Guide for WaveTrend V4.1

**Date**: 2025-12-22
**Purpose**: Enable restart-resilient trading with Redis persistence

---

## Why Redis Persistence?

### The Problem Without Persistence

**Default behavior** (no database):
```
Strategy starts → Trade → Position opens → Bot restarts
                                        ↓
                            Cache CLEARED (in-memory only)
                                        ↓
            Reconciliation fetches from exchange... but:
            - FUTURES: ✅ Gets position with entry price
            - SPOT: ❌ Only gets balance (no entry price!)
```

### The Solution With Redis

**With Redis persistence**:
```
Strategy starts → Trade → Position opens → Redis snapshots state
                                                    ↓
                                        Bot restarts
                                                    ↓
                              Redis restores: entry_price, peak_price
                                                    ↓
                              Reconciliation fetches orders/positions
                                                    ↓
                              ✅ Full state restored!
```

---

## Redis vs PostgreSQL: What's the Difference?

### Current NautilusTrader Architecture

| Component | Database | Purpose | Speed |
|-----------|----------|---------|-------|
| **Cache** (positions, orders) | Redis | Live trading state | ⚡ Ultra-fast (in-memory) |
| **MessageBus** (events) | Redis | Real-time event streams | ⚡ Ultra-fast |
| **ParquetDataCatalog** | PostgreSQL | Historical data storage | 🐢 Slower (disk-based) |

**The truth**: Currently, **only Redis is supported** for `CacheConfig` and `MessageBusConfig`. PostgreSQL is used separately for the data catalog (backtesting data, historical bars/ticks).

**Future**: PostgreSQL support for cache/message bus may come, but Redis is the production-ready choice today.

---

## When Do You NEED Redis?

### FUTURES Trading

**Without Redis**:
- ✅ Reconciliation gets position from exchange API
- ✅ Entry price available via `GET /fapi/v1/positionRisk`
- ⚠️ But: Better to have Redis for audit trails

**Verdict**: **Recommended but not critical**

### SPOT Trading

**Without Redis**:
- ❌ Reconciliation only gets balances (no entry price)
- ❌ Cannot calculate P&L correctly
- ❌ Trailing stops have wrong reference price

**Verdict**: **REQUIRED for SPOT trading**

---

## Setup Instructions

### 1. Start Redis

**Option A: Docker (easiest)**:
```bash
# Standalone Redis container
docker run -d -p 6379:6379 --name nautilus-redis redis

# OR use NautilusTrader's docker-compose (includes PostgreSQL too)
make start-services  # Starts Redis + PostgreSQL + pgAdmin
```

**Option B: Native installation**:
```bash
# Ubuntu/Debian
sudo apt install redis-server
sudo systemctl start redis

# macOS
brew install redis
brew services start redis

# Verify running
redis-cli ping  # Should return: PONG
```

### 2. Configure Strategy

Use the Redis-enabled example:

```bash
# Copy the Redis-enabled configuration
cp examples/live/binance/binance_futures_testnet_wavetrend_v4_1_redis.py \
   my_live_strategy.py

# Edit configuration if Redis is on different host/port
# (default: localhost:6379)
```

### 3. Set Environment Variables

```bash
# For testnet
export BINANCE_FUTURES_TESTNET_API_KEY=your_testnet_key
export BINANCE_FUTURES_TESTNET_API_SECRET=your_testnet_secret

# For production (CAREFUL!)
export BINANCE_API_KEY=your_production_key
export BINANCE_API_SECRET=your_production_secret
```

### 4. Run

```bash
python my_live_strategy.py
```

**Expected logs on startup**:
```
[INFO] Connected to Redis at localhost:6379
[INFO] Cache database initialized
[INFO] Starting WaveTrendMultiTimeframeV4_1
[INFO] Started with no existing positions  # First run
```

**On restart (with open position)**:
```
[INFO] Connected to Redis at localhost:6379
[INFO] Cache restored from database
[WARN] RECONNECTION: Found existing position (LONG 0.01 @ 95000.0...)
[INFO] RECONNECTION: Adopted existing stop order @ 92150.0
```

---

## Configuration Deep Dive

### Redis Database Config

```python
from nautilus_trader.common.config import DatabaseConfig

redis_config = DatabaseConfig(
    type="redis",           # Only option currently
    host="localhost",       # Redis host
    port=6379,             # Redis port (default)
    username=None,         # Optional: Redis 6+ ACL username
    password=None,         # Optional: Redis AUTH password
    ssl=False,             # Set True for TLS connection
    timeout=20,            # Connection timeout (seconds)
)
```

### Cache Config (Positions, Orders, Account State)

```python
from nautilus_trader.cache.config import CacheConfig

cache_config = CacheConfig(
    database=redis_config,
    encoding="msgpack",    # msgpack > json (faster, smaller)
    timestamps_as_iso8601=True,
    buffer_interval_ms=100,  # Write every 100ms (batched)
    flush_on_start=False,    # ⚠️ CRITICAL: Don't clear Redis!
    persist_account_events=True,  # Track all account changes
    use_trader_prefix=True,  # Keys: trader-{trader_id}:...
    use_instance_id=False,   # Don't use instance ID in keys
)
```

**Key setting**: `flush_on_start=False`
- `True`: Clears Redis on every startup ❌ (loses state!)
- `False`: Preserves state across restarts ✅

### Message Bus Config (Event Streams)

```python
from nautilus_trader.common.config import MessageBusConfig

message_bus_config = MessageBusConfig(
    database=redis_config,
    encoding="msgpack",
    timestamps_as_iso8601=True,
    buffer_interval_ms=100,
    autotrim_mins=60,       # Auto-delete streams older than 1 hour
    use_trader_prefix=True,
    use_trader_id=True,
    use_instance_id=False,
    streams_prefix="wavetrend",  # Redis key prefix
    stream_per_topic=True,  # One stream per event type
    # types_filter=[QuoteTick],  # Optionally exclude types
)
```

**What gets streamed**:
- Order events (submitted, filled, canceled)
- Position events (opened, changed, closed)
- Account events (balance changes)
- Trade fills
- Custom strategy events

**Use cases**:
- Real-time monitoring dashboard
- Post-trade analysis
- Compliance audit trails
- Event replay for testing

### Execution Engine Config (Snapshots)

```python
from nautilus_trader.config import LiveExecEngineConfig

exec_engine=LiveExecEngineConfig(
    reconciliation=True,
    reconciliation_lookback_mins=1440,  # 24 hours
    snapshot_orders=True,           # ✅ Snapshot to Redis
    snapshot_positions=True,        # ✅ Snapshot to Redis
    snapshot_positions_interval_secs=5.0,  # Every 5 seconds
)
```

**Snapshot behavior**:
- Every 5 seconds: Position state saved to Redis
- Includes: entry_price, quantity, unrealized P&L, peak_price
- On restart: Restored before reconciliation runs

---

## Redis Data Structure

### What's Stored in Redis

**Cache keys** (with `trader-WAVETREND-TESTNET-001:` prefix):

```
trader-{id}:cache:general        # General cache metadata
trader-{id}:cache:currencies     # Currency definitions
trader-{id}:cache:instruments    # Instrument definitions
trader-{id}:cache:accounts       # Account balances
trader-{id}:cache:orders:open    # Open orders
trader-{id}:cache:orders:closed  # Closed orders (recent)
trader-{id}:cache:positions:open # Open positions
trader-{id}:cache:positions:closed  # Closed positions (recent)
```

**Message bus streams**:

```
wavetrend:trader-{id}:OrderEvent
wavetrend:trader-{id}:PositionEvent
wavetrend:trader-{id}:AccountEvent
wavetrend:trader-{id}:FillEvent
```

### Viewing Redis Data

**Connect to Redis CLI**:
```bash
redis-cli

# List all keys
127.0.0.1:6379> KEYS trader-*

# View a specific position
127.0.0.1:6379> GET trader-WAVETREND-TESTNET-001:cache:positions:open

# View stream length
127.0.0.1:6379> XLEN wavetrend:trader-WAVETREND-TESTNET-001:OrderEvent

# Read last 10 events from stream
127.0.0.1:6379> XREVRANGE wavetrend:trader-WAVETREND-TESTNET-001:OrderEvent + - COUNT 10
```

---

## Production Best Practices

### 1. Redis Persistence (Disk Snapshots)

**Redis by default** is in-memory only. Enable disk persistence:

**Edit `/etc/redis/redis.conf`**:
```conf
# Enable RDB snapshots
save 900 1      # Save if 1 key changed in 15 minutes
save 300 10     # Save if 10 keys changed in 5 minutes
save 60 10000   # Save if 10000 keys changed in 1 minute

# Enable AOF (append-only file) for durability
appendonly yes
appendfsync everysec  # Flush to disk every second
```

**Restart Redis**:
```bash
sudo systemctl restart redis
```

### 2. Redis High Availability

**For production**, use Redis Sentinel or Redis Cluster:

```python
# Redis Sentinel configuration (failover)
redis_config = DatabaseConfig(
    type="redis",
    host="redis-sentinel-service",  # Sentinel service endpoint
    port=26379,  # Sentinel port (not 6379)
    username="default",
    password="your_redis_password",
    ssl=True,  # Use TLS in production
    timeout=20,
)
```

### 3. Monitoring

**Track Redis health**:
```bash
# Memory usage
redis-cli INFO memory

# Connected clients
redis-cli INFO clients

# Keyspace statistics
redis-cli INFO keyspace

# Monitor live commands (debugging)
redis-cli MONITOR
```

**Alert on**:
- Memory usage > 80%
- Connection failures
- Slow commands (> 10ms)

### 4. Security

**Production checklist**:
- ✅ Enable `requirepass` (Redis AUTH)
- ✅ Use TLS/SSL for connections
- ✅ Bind to localhost or private network (not 0.0.0.0)
- ✅ Use Redis ACLs (Redis 6+) for fine-grained permissions
- ✅ Firewall rules to restrict access

---

## Troubleshooting

### "Connection refused" Error

**Problem**: Redis not running

**Solution**:
```bash
# Check if Redis is running
redis-cli ping

# Start Redis
docker start nautilus-redis
# OR
sudo systemctl start redis
```

### "NOAUTH Authentication required"

**Problem**: Redis requires password but none provided

**Solution**:
```python
redis_config = DatabaseConfig(
    type="redis",
    host="localhost",
    port=6379,
    password="your_redis_password",  # Add password
)
```

### Position State Not Restored After Restart

**Problem**: `flush_on_start=True` clearing Redis

**Solution**:
```python
cache_config = CacheConfig(
    database=redis_config,
    flush_on_start=False,  # ✅ Change to False
)
```

### Redis Memory Full

**Problem**: Redis maxmemory limit reached

**Solution**:
```bash
# Increase maxmemory in redis.conf
maxmemory 2gb

# Set eviction policy
maxmemory-policy allkeys-lru  # Evict least recently used keys
```

**OR** enable `autotrim_mins` in MessageBusConfig:
```python
message_bus_config = MessageBusConfig(
    autotrim_mins=60,  # Auto-delete streams older than 1 hour
)
```

---

## Testing the Setup

### 1. Start Redis

```bash
make start-services
```

### 2. Run Strategy

```bash
python examples/live/binance/binance_futures_testnet_wavetrend_v4_1_redis.py
```

### 3. Verify Redis Connection

**In another terminal**:
```bash
redis-cli

# Check keys are being created
127.0.0.1:6379> KEYS trader-*

# Watch live updates (Ctrl+C to stop)
127.0.0.1:6379> MONITOR
```

### 4. Test Restart Resilience

**While strategy has an open position**:

1. Press Ctrl+C (graceful shutdown)
2. Check Redis still has data:
   ```bash
   redis-cli KEYS trader-*  # Should show keys
   ```
3. Restart strategy:
   ```bash
   python examples/live/binance/binance_futures_testnet_wavetrend_v4_1_redis.py
   ```
4. Look for reconnection logs:
   ```
   [WARN] RECONNECTION: Found existing position...
   [INFO] RECONNECTION: Adopted existing stop order...
   ```

✅ **Success**: Position and stops adopted from previous session!

---

## Cost and Resource Usage

### Redis Memory Requirements

**Per trading session** (rough estimates):

```
Instruments: ~10 KB each
Open orders: ~1 KB each
Positions: ~2 KB each
Account state: ~5 KB
Message streams: ~100 bytes per event

Example for 1 strategy:
- 1 instrument: 10 KB
- 1 position: 2 KB
- 2 orders (entry + stop): 2 KB
- 1000 events in stream: 100 KB
Total: ~115 KB
```

**Recommendation**: 256 MB Redis instance handles 2000+ trading sessions easily.

**Production**: 1-2 GB Redis instance for multiple strategies + historical streams.

### Performance Impact

**Latency added**:
- Order submission: +0.5-2ms (Redis write)
- Position update: +0.5-2ms (Redis write)

**Throughput**:
- Redis handles 100,000+ writes/second
- Batching (100ms buffer) reduces Redis calls 10-100x

**Verdict**: Negligible impact for typical trading frequencies (< 10 orders/second).

---

## Summary

### For FUTURES Trading

**Without Redis**: ✅ Works (reconciliation gets entry price from exchange)
**With Redis**: ✅✅ Better (audit trails, event replay, guaranteed state preservation)

**Recommendation**: **Use Redis** for production (small overhead, huge benefits)

### For SPOT Trading

**Without Redis**: ❌ Broken (no entry price on restart)
**With Redis**: ✅ Works (entry price restored from snapshot)

**Recommendation**: **Redis is REQUIRED** for SPOT trading

### Next Steps

1. ✅ Start Redis: `make start-services`
2. ✅ Use Redis example: `binance_futures_testnet_wavetrend_v4_1_redis.py`
3. ✅ Test restart resilience (Ctrl+C → restart → verify reconnection)
4. ✅ Monitor Redis: `redis-cli MONITOR`
5. ✅ Deploy to production with persistence enabled in redis.conf

---

**Implementation Complete**: 2025-12-22
**Status**: ✅ Production-ready with Redis persistence

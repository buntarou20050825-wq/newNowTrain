"""
Realtime Train SSE Server (FastAPI) - 完全版
-------------------------------------------
機能:
- odpt:Train + odpt:TrainTimetable で精密な位置補間
- odpt:Station で駅座標キャッシュ
- GTFS形式でフロントに配信
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import math

load_dotenv()

# 設定
ODPT_BASE = os.getenv("ODPT_BASE", "https://api-challenge.odpt.org/api/v4")
ODPT_KEY = os.getenv("ODPT_CONSUMER_KEY", "")
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "3"))
SSE_HEARTBEAT_SEC = int(os.getenv("SSE_HEARTBEAT_SEC", "1"))
TTL_SEC = int(os.getenv("TTL_SEC", "15"))

# 正規化ユーティリティ
def normalize_railway_id(raw: str) -> str:
    if not raw:
        return raw
    if raw.startswith("odpt.Railway:"):
        return raw.split(":", 1)[1]
    return raw

def normalize_trip_id(raw: str) -> str:
    if not raw:
        return raw
    if raw.startswith("odpt.Train:"):
        raw = raw.split(":", 1)[1]
    # 末尾の .YYYYMMDD を削除
    parts = raw.split(".")
    if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) == 8:
        parts = parts[:-1]
    return ".".join(parts)

def time_to_seconds(time_str: str) -> int:
    """HH:mm:ss を秒に変換（24時超対応）"""
    if not time_str:
        return 0
    parts = time_str.split(":")
    h = int(parts[0])
    m = int(parts[1])
    s = int(parts[2]) if len(parts) > 2 else 0
    return h * 3600 + m * 60 + s

def unix_ts() -> int:
    return int(time.time())

# スキーマ
class Vehicle(BaseModel):
    trip_id: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    status: Optional[str] = None
    bearing: Optional[float] = None
    speed_kph: Optional[float] = None
    from_stop_id: Optional[str] = None
    to_stop_id: Optional[str] = None
    timestamp: Optional[int] = None
    progress: Optional[float] = None
    interpolated: Optional[bool] = None  # GTFS補間されたかどうか

class Snapshot(BaseModel):
    ts: int
    seq: int
    railwayId: Optional[str] = None
    vehicles: List[Vehicle]

# ODPT API
async def odpt_get(client: httpx.AsyncClient, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = {"acl:consumerKey": ODPT_KEY}
    p.update(params)
    url = f"{ODPT_BASE}/{path}"
    r = await client.get(url, params=p, timeout=30)
    r.raise_for_status()
    return r.json()

async def fetch_odpt_stations(client: httpx.AsyncClient, railway_id: Optional[str] = None) -> List[Dict[str, Any]]:
    params = {}
    if railway_id:
        params["odpt:railway"] = railway_id
    return await odpt_get(client, "odpt:Station", params)

async def fetch_odpt_trains(client: httpx.AsyncClient, railway_id: Optional[str] = None) -> List[Dict[str, Any]]:
    params = {}
    if railway_id:
        params["odpt:railway"] = railway_id
    return await odpt_get(client, "odpt:Train", params)

async def fetch_odpt_timetables(client: httpx.AsyncClient, railway_id: str, calendar: str = "Weekday") -> List[Dict[str, Any]]:
    params = {
        "odpt:railway": railway_id,
        "odpt:calendar": f"odpt.Calendar:{calendar}"
    }
    return await odpt_get(client, "odpt:TrainTimetable", params)

# GTFS統合システム
class GTFSLoader:
    """GTFS静的データローダー"""
    def __init__(self, gtfs_dir: str = "./train_json"):
        from pathlib import Path
        self.gtfs_dir = Path(gtfs_dir)
        self.stops = {}           # stop_id → {lat, lng, name}
        self.trips = {}           # trip_id → {route_id, headsign}
        self.stop_times = {}      # trip_id → [{stop_id, arrival, departure, sequence}, ...]
        self.routes = {}          # route_id → {name, color}

        self.load_all()

    def load_all(self):
        """全GTFSファイルを読み込み"""
        print("[GTFS] Loading static data...")

        try:
            # stops.json
            with open(self.gtfs_dir / "stops.json", encoding="utf-8") as f:
                stops_data = json.load(f)
                for stop in stops_data:
                    self.stops[stop["stop_id"]] = {
                        "lat": float(stop["stop_lat"]),
                        "lng": float(stop["stop_lon"]),
                        "name": stop["stop_name"]
                    }
            print(f"[GTFS] Loaded {len(self.stops)} stops")
        except Exception as e:
            print(f"[GTFS] Failed to load stops.json: {e}")

        try:
            # routes.json
            with open(self.gtfs_dir / "routes.json", encoding="utf-8") as f:
                routes_data = json.load(f)
                for route in routes_data:
                    self.routes[route["route_id"]] = {
                        "name": route.get("route_long_name", route.get("route_short_name", "")),
                        "color": f"#{route['route_color']}" if "route_color" in route else "#4CAF50"
                    }
            print(f"[GTFS] Loaded {len(self.routes)} routes")
        except Exception as e:
            print(f"[GTFS] Failed to load routes.json: {e}")

        try:
            # trips.json
            with open(self.gtfs_dir / "trips.json", encoding="utf-8") as f:
                trips_data = json.load(f)
                for trip in trips_data:
                    self.trips[trip["trip_id"]] = {
                        "route_id": trip["route_id"],
                        "headsign": trip.get("trip_headsign", "")
                    }
            print(f"[GTFS] Loaded {len(self.trips)} trips")
        except Exception as e:
            print(f"[GTFS] Failed to load trips.json: {e}")

        try:
            # stop_times.json
            print("[GTFS] Loading stop_times.json (this may take a while)...")
            with open(self.gtfs_dir / "stop_times.json", encoding="utf-8") as f:
                stop_times_data = json.load(f)
                for st in stop_times_data:
                    trip_id = st["trip_id"]
                    if trip_id not in self.stop_times:
                        self.stop_times[trip_id] = []

                    # arrival_timeやdeparture_timeが空文字列の場合はスキップ
                    arrival = st.get("arrival_time", "")
                    departure = st.get("departure_time", "")

                    self.stop_times[trip_id].append({
                        "stop_id": st["stop_id"],
                        "arrival_time": arrival if arrival else None,
                        "departure_time": departure if departure else None,
                        "stop_sequence": int(st["stop_sequence"])
                    })

            # stop_timesをsequenceでソート
            for trip_id in self.stop_times:
                self.stop_times[trip_id].sort(key=lambda x: x["stop_sequence"])

            print(f"[GTFS] Loaded stop_times for {len(self.stop_times)} trips")
        except Exception as e:
            print(f"[GTFS] Failed to load stop_times.json: {e}")
            import traceback
            traceback.print_exc()

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の距離をkmで計算"""
    R = 6371  # 地球の半径(km)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

class StationMapper:
    """ODPT駅ID ↔ GTFS駅IDマッパー"""
    def __init__(self, gtfs_loader: GTFSLoader, odpt_stations: Dict):
        """
        odpt_stations = {
            "odpt.Station:JR-East.Chuo.Shinjuku": {"lat": 35.xxx, "lon": 139.xxx, "name": "新宿"},
            ...
        }
        """
        self.gtfs_stops = gtfs_loader.stops
        self.odpt_stations = odpt_stations
        self.odpt_to_gtfs = {}  # odpt_id → gtfs_stop_id

        self.create_mapping()

    def create_mapping(self, max_distance_km: float = 0.3):
        """座標ベースで駅をマッピング（300m以内）"""
        print("[Mapper] Creating station mapping...")

        for odpt_id, odpt_station in self.odpt_stations.items():
            best_match = None
            best_distance = float('inf')

            for gtfs_id, gtfs_stop in self.gtfs_stops.items():
                distance = haversine_distance(
                    odpt_station["lat"], odpt_station["lon"],
                    gtfs_stop["lat"], gtfs_stop["lng"]
                )

                if distance < best_distance and distance < max_distance_km:
                    best_distance = distance
                    best_match = gtfs_id

            if best_match:
                self.odpt_to_gtfs[odpt_id] = best_match

        print(f"[Mapper] Mapped {len(self.odpt_to_gtfs)} stations")

    def get_gtfs_stop_id(self, odpt_station_id: str) -> Optional[str]:
        """ODPT駅ID → GTFS駅IDに変換"""
        return self.odpt_to_gtfs.get(odpt_station_id)

class TripMatcher:
    """リアルタイムtrip_id ↔ GTFS静的trip_idマッチャー"""
    def __init__(self, gtfs_loader: GTFSLoader, station_mapper: StationMapper):
        self.gtfs_loader = gtfs_loader
        self.station_mapper = station_mapper
        self.cache = {}  # rt_trip_id → static_trip_id のキャッシュ

    def time_to_seconds(self, time_str: str) -> int:
        """HH:MM:SS を秒に変換（24:00:00以降も対応）"""
        if not time_str:
            return 0
        parts = time_str.split(":")
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
        return h * 3600 + m * 60 + s

    def extract_train_number(self, rt_trip_id: str) -> str:
        """JR-East.Chuo.554M → 554M"""
        return rt_trip_id.split(".")[-1]

    def find_best_match(
        self,
        rt_trip_id: str,
        current_time_sec: int,
        from_stop_odpt: Optional[str],
        to_stop_odpt: Optional[str]
    ) -> Optional[str]:
        """
        最適なGTFS trip_idを見つける

        Args:
            rt_trip_id: "JR-East.Chuo.554M"
            current_time_sec: 現在時刻の秒数（例: 16:15 → 58500）
            from_stop_odpt: "odpt.Station:JR-East.Chuo.Shinjuku"
            to_stop_odpt: "odpt.Station:JR-East.Chuo.Nakano"

        Returns:
            GTFS trip_id (例: "1610554M") or None
        """

        # キャッシュチェック
        if rt_trip_id in self.cache:
            return self.cache[rt_trip_id]

        # 列車番号抽出
        train_number = self.extract_train_number(rt_trip_id)

        # 候補抽出（列車番号を含むtrip）
        candidates = [
            trip_id for trip_id in self.gtfs_loader.stop_times.keys()
            if train_number in trip_id
        ]

        if not candidates:
            return None

        # ODPT駅ID → GTFS駅IDに変換
        from_stop_gtfs = self.station_mapper.get_gtfs_stop_id(from_stop_odpt) if from_stop_odpt else None
        to_stop_gtfs = self.station_mapper.get_gtfs_stop_id(to_stop_odpt) if to_stop_odpt else None

        # スコアリング
        best_trip_id = None
        best_score = -float('inf')

        for candidate_id in candidates:
            stop_times = self.gtfs_loader.stop_times[candidate_id]

            if not stop_times:
                continue

            # 最初の発車時刻
            first_stop = stop_times[0]
            first_time_str = first_stop.get("departure_time") or first_stop.get("arrival_time")
            if not first_time_str:
                continue

            first_departure = self.time_to_seconds(first_time_str)

            # 時刻の近さ（近いほど高スコア）
            time_diff = abs(current_time_sec - first_departure)
            score = -time_diff

            # 駅マッチング
            stop_ids = [st["stop_id"] for st in stop_times]

            idx_from = -1
            idx_to = -1

            if from_stop_gtfs:
                try:
                    idx_from = stop_ids.index(from_stop_gtfs)
                    score += 10000  # from駅一致ボーナス
                except ValueError:
                    pass

            if to_stop_gtfs:
                try:
                    idx_to = stop_ids.index(to_stop_gtfs)
                    score += 10000  # to駅一致ボーナス
                except ValueError:
                    pass

            # 駅順序が正しいか
            if idx_from >= 0 and idx_to >= 0 and idx_from < idx_to:
                score += 1000  # 順序正しいボーナス

                # 現在時刻が区間内にあるか
                from_stop = stop_times[idx_from]
                to_stop = stop_times[idx_to]

                dep_time_str = from_stop.get("departure_time")
                arr_time_str = to_stop.get("arrival_time")

                if dep_time_str and arr_time_str:
                    dep_time = self.time_to_seconds(dep_time_str)
                    arr_time = self.time_to_seconds(arr_time_str)

                    if dep_time <= current_time_sec <= arr_time:
                        score += 500  # 区間内ボーナス

            if score > best_score:
                best_score = score
                best_trip_id = candidate_id

        if best_trip_id:
            self.cache[rt_trip_id] = best_trip_id
            print(f"[Matcher] {rt_trip_id} → {best_trip_id} (score: {best_score})")

        return best_trip_id

    def interpolate_position(
        self,
        static_trip_id: str,
        current_time_sec: int,
        from_stop_odpt: str,
        to_stop_odpt: str,
        delay_sec: int = 0
    ) -> Optional[Dict]:
        """
        時刻表ベースで駅間位置を補間

        Returns:
            {
                "lat": float,
                "lng": float,
                "progress": float (0.0~1.0),
                "from_stop_gtfs": str,
                "to_stop_gtfs": str
            }
        """

        stop_times = self.gtfs_loader.stop_times.get(static_trip_id)
        if not stop_times:
            return None

        # ODPT → GTFS 変換
        from_stop_gtfs = self.station_mapper.get_gtfs_stop_id(from_stop_odpt)
        to_stop_gtfs = self.station_mapper.get_gtfs_stop_id(to_stop_odpt)

        if not from_stop_gtfs or not to_stop_gtfs:
            return None

        # 該当する区間を検索
        idx_from = -1
        idx_to = -1

        for i, st in enumerate(stop_times):
            if st["stop_id"] == from_stop_gtfs:
                idx_from = i
            if st["stop_id"] == to_stop_gtfs:
                idx_to = i

        if idx_from < 0 or idx_to < 0 or idx_from >= idx_to:
            return None

        # 時刻取得
        from_stop = stop_times[idx_from]
        to_stop = stop_times[idx_to]

        dep_time_str = from_stop.get("departure_time") or from_stop.get("arrival_time")
        arr_time_str = to_stop.get("arrival_time") or to_stop.get("departure_time")

        if not dep_time_str or not arr_time_str:
            return None

        dep_time = self.time_to_seconds(dep_time_str)
        arr_time = self.time_to_seconds(arr_time_str)

        # 遅延を加算
        dep_time += delay_sec
        arr_time += delay_sec

        # 進捗率計算
        duration = arr_time - dep_time
        if duration <= 0:
            return None

        progress = (current_time_sec - dep_time) / duration
        progress = max(0.0, min(1.0, progress))  # 0~1にクランプ

        # 駅座標取得
        from_pos = self.gtfs_loader.stops.get(from_stop_gtfs)
        to_pos = self.gtfs_loader.stops.get(to_stop_gtfs)

        if not from_pos or not to_pos:
            return None

        # 線形補間
        lat = from_pos["lat"] + (to_pos["lat"] - from_pos["lat"]) * progress
        lng = from_pos["lng"] + (to_pos["lng"] - from_pos["lng"]) * progress

        return {
            "lat": lat,
            "lng": lng,
            "progress": progress,
            "from_stop_gtfs": from_stop_gtfs,
            "to_stop_gtfs": to_stop_gtfs
        }

# キャッシュ
class DataCache:
    def __init__(self) -> None:
        self.seq = 0
        self.vehicles_by_railway: Dict[str, List[Vehicle]] = {}
        self.last_seen_by_trip: Dict[str, int] = {}
        
        # 駅キャッシュ: {station_id: {lat, lng, name}}
        self.stations: Dict[str, Dict[str, Any]] = {}
        
        # 時刻表キャッシュ: {trip_id: {stops: [{stop_id, arrival, departure, sequence}]}}
        self.timetables: Dict[str, Dict[str, Any]] = {}

    def snapshot(self, railway_id_norm: Optional[str]) -> Snapshot:
        self.seq += 1
        now = unix_ts()
        vehicles: List[Vehicle]
        if railway_id_norm:
            items = self.vehicles_by_railway.get(railway_id_norm, [])
            vehicles = [v for v in items if v.timestamp is None or now - v.timestamp <= TTL_SEC]
        else:
            all_items: List[Vehicle] = []
            for arr in self.vehicles_by_railway.values():
                all_items.extend(arr)
            vehicles = [v for v in all_items if v.timestamp is None or now - v.timestamp <= TTL_SEC]
        return Snapshot(ts=now, seq=self.seq, railwayId=railway_id_norm, vehicles=vehicles)

cache = DataCache()
client = httpx.AsyncClient()
app = FastAPI()

# GTFS統合システムのグローバル変数
gtfs_loader: Optional[GTFSLoader] = None
station_mapper: Optional[StationMapper] = None
trip_matcher: Optional[TripMatcher] = None

# CORS設定を追加
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 開発用: すべてのオリジンを許可
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 位置補間関数
def interpolate_position(
    from_station: Dict[str, Any],
    to_station: Dict[str, Any],
    timetable_stops: List[Dict[str, Any]],
    current_time_sec: int,
    delay_sec: int
) -> Optional[Dict[str, float]]:
    """駅間の位置を時刻表ベースで補間"""
    
    # from/to の駅IDを探す
    from_idx = None
    to_idx = None
    
    for i, stop in enumerate(timetable_stops):
        if stop["stop_id"] == from_station.get("id"):
            from_idx = i
        if stop["stop_id"] == to_station.get("id"):
            to_idx = i
    
    if from_idx is None or to_idx is None or from_idx >= to_idx:
        return None
    
    from_stop = timetable_stops[from_idx]
    to_stop = timetable_stops[to_idx]
    
    # 発車時刻と到着時刻
    dep_time = time_to_seconds(from_stop.get("departure") or from_stop.get("arrival", "00:00:00"))
    arr_time = time_to_seconds(to_stop.get("arrival", "00:00:00"))
    
    if arr_time <= dep_time:
        return None
    
    # 遅延を反映
    adjusted_current = current_time_sec
    adjusted_dep = dep_time + delay_sec
    adjusted_arr = arr_time + delay_sec
    
    # 進捗率計算
    if adjusted_current < adjusted_dep:
        progress = 0.0
    elif adjusted_current > adjusted_arr:
        progress = 1.0
    else:
        progress = (adjusted_current - adjusted_dep) / (adjusted_arr - adjusted_dep)
    
    # 座標補間
    from_lat = from_station.get("lat")
    from_lng = from_station.get("lng")
    to_lat = to_station.get("lat")
    to_lng = to_station.get("lng")
    
    if None in [from_lat, from_lng, to_lat, to_lng]:
        return None
    
    lat = from_lat + (to_lat - from_lat) * progress
    lng = from_lng + (to_lng - from_lng) * progress
    
    return {"lat": lat, "lng": lng, "progress": progress}

# データ変換
def map_odpt_trains_to_vehicles(items: List[Dict[str, Any]]) -> List[Vehicle]:
    out: List[Vehicle] = []
    now = unix_ts()
    
    # 現在の時刻（秒）- 今日の00:00からの経過秒数
    current_dt = datetime.now()
    current_time_sec = current_dt.hour * 3600 + current_dt.minute * 60 + current_dt.second
    
    for idx, it in enumerate(items):
        # odpt:train でtrip_idを取得（最優先）
        trip_id_raw = it.get("odpt:train") or it.get("owl:sameAs") or ""
        if not trip_id_raw:
            continue
        
        trip_id = normalize_trip_id(str(trip_id_raw))
        
        # 駅情報
        from_station_id = it.get("odpt:fromStation")
        to_station_id = it.get("odpt:toStation")
        
        # 遅延（秒）
        delay = it.get("odpt:delay", 0)
        
        # タイムスタンプ
        ts_iso = it.get("dct:valid") or it.get("dc:date")
        ts_epoch = now
        if ts_iso:
            try:
                ts_iso_clean = ts_iso.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts_iso_clean)
                ts_epoch = int(dt.timestamp())
            except Exception:
                pass
        
        # 状態判定
        if to_station_id is None:
            # 駅に停車中
            status = "STOPPED_AT"
            station = cache.stations.get(from_station_id)
            if station:
                lat = station["lat"]
                lng = station["lng"]
                progress = 0.0
            else:
                lat = None
                lng = None
                progress = 0.0
        else:
            # 駅間移動中
            status = "IN_TRANSIT_TO"
            
            # 時刻表から位置を補間
            timetable = cache.timetables.get(trip_id_raw) or cache.timetables.get(trip_id)
            from_station = cache.stations.get(from_station_id)
            to_station = cache.stations.get(to_station_id)
            
            if timetable and from_station and to_station:
                pos = interpolate_position(
                    from_station,
                    to_station,
                    timetable["stops"],
                    current_time_sec,
                    delay
                )
                if pos:
                    lat = pos["lat"]
                    lng = pos["lng"]
                    progress = pos["progress"]
                else:
                    # フォールバック: 中間地点
                    lat = (from_station["lat"] + to_station["lat"]) / 2
                    lng = (from_station["lng"] + to_station["lng"]) / 2
                    progress = 0.5
            else:
                # 駅座標だけある場合は中間地点
                if from_station and to_station:
                    lat = (from_station["lat"] + to_station["lat"]) / 2
                    lng = (from_station["lng"] + to_station["lng"]) / 2
                    progress = 0.5
                else:
                    lat = None
                    lng = None
                    progress = 0.0
        
        out.append(
            Vehicle(
                trip_id=trip_id,
                lat=lat,
                lng=lng,
                status=status,
                from_stop_id=from_station_id,
                to_stop_id=to_station_id,
                timestamp=ts_epoch,
                progress=progress
            )
        )
    
    return out

# ポーリングループ
async def poll_loop() -> None:
    global gtfs_loader, station_mapper, trip_matcher

    await asyncio.sleep(0.2)
    print("=" * 60)
    print("[poll_loop] STARTED!")
    print("=" * 60)

    # GTFSシステムの初期化
    try:
        print("[Startup] Initializing GTFS system...")

        # GTFSローダー（複数のパスを試す）
        possible_paths = ["./train_json", "../train_json", "/home/user/newNowTrain/train_json"]
        gtfs_path = None
        for path in possible_paths:
            from pathlib import Path
            if Path(path).exists():
                gtfs_path = path
                print(f"[Startup] Found GTFS data at: {path}")
                break

        if not gtfs_path:
            print("[Startup] WARNING: train_json directory not found!")
            gtfs_loader = None
        else:
            gtfs_loader = GTFSLoader(gtfs_path)

        # ODPT駅情報取得
        print("[Startup] Loading ODPT station data...")
        odpt_stations = {}
        stations = await fetch_odpt_stations(client, None)
        for station in stations:
            station_id = station.get("owl:sameAs")
            lat = station.get("geo:lat")
            lng = station.get("geo:long") or station.get("geo:lon")
            name = station.get("dc:title") or station.get("odpt:stationTitle", {}).get("ja", "")
            if station_id and lat and lng:
                cache.stations[station_id] = {"id": station_id, "lat": lat, "lng": lng, "name": name}
                odpt_stations[station_id] = {"lat": lat, "lon": lng, "name": name}
        print(f"[Startup] Loaded {len(cache.stations)} ODPT stations")

        # 駅マッピング
        if gtfs_loader:
            station_mapper = StationMapper(gtfs_loader, odpt_stations)

            # trip マッチャー
            trip_matcher = TripMatcher(gtfs_loader, station_mapper)

            print("[Startup] GTFS system ready!")
        else:
            print("[Startup] GTFS system not initialized (train_json not found)")
    except Exception as e:
        print(f"[Startup] Failed to initialize GTFS system: {e}")
        import traceback
        traceback.print_exc()
    
    # 起動時に時刻表を取得（JR東日本の主要路線）
    target_railways = [
        "odpt.Railway:JR-East.ChuoSobuLocal",
        "odpt.Railway:JR-East.ChuoRapid",
        "odpt.Railway:JR-East.Yamanote",
    ]
    
    for railway_id in target_railways:
        try:
            print(f"[poll_loop] Loading timetables for {railway_id}...")
            timetables = await fetch_odpt_timetables(client, railway_id, "Weekday")
            
            for tt in timetables:
                trip_id_raw = tt.get("odpt:train")
                if not trip_id_raw:
                    continue
                
                stops = []
                for i, obj in enumerate(tt.get("odpt:trainTimetableObject", [])):
                    # デバッグ: 最初の1件だけキーを出力
                    if i == 0:
                        print(f"[DEBUG] First timetable object keys: {list(obj.keys())}")
                        print(f"[DEBUG] Sample object: {obj}")
                    
                    stops.append({
                        "stop_id": obj.get("odpt:station"),
                        "arrival": obj.get("odpt:arrivalTime"),
                        "departure": obj.get("odpt:departureTime"),
                        "sequence": i + 1
                    })
                
                cache.timetables[trip_id_raw] = {"stops": stops}
                # 正規化版でも登録
                trip_id_norm = normalize_trip_id(trip_id_raw)
                cache.timetables[trip_id_norm] = {"stops": stops}
            
            print(f"[poll_loop] Loaded {len(timetables)} timetables for {railway_id}")
        except Exception as e:
            print(f"[poll_loop] Failed to load timetables for {railway_id}: {e}")
    
    # ポーリングループ
    while True:
        try:
            print("\n" + "=" * 60)
            print(f"[poll_loop] Polling at {datetime.now()}")
            print("=" * 60)
            
            railways = [None]  # 全路線取得
            for railway_filter in railways:
                odpt_trains = await fetch_odpt_trains(client, railway_filter)
                print(f"[poll_loop] ODPT returned: {len(odpt_trains)} trains")

                v_merged = map_odpt_trains_to_vehicles(odpt_trains)
                print(f"[poll_loop] Converted to {len(v_merged)} vehicles")

                # GTFS補間を実行（既存の時刻表ベース補間の後）
                if trip_matcher:
                    interpolated_count = 0
                    current_dt = datetime.now()
                    current_time_sec = current_dt.hour * 3600 + current_dt.minute * 60 + current_dt.second

                    for vehicle in v_merged:
                        # 既に座標がある場合はスキップ（既存の補間が成功している）
                        if vehicle.lat is not None and vehicle.lng is not None:
                            continue

                        # GTFS-RTに座標がない、かつfrom/to駅情報がある場合、GTFS静的データで補間
                        if vehicle.from_stop_id and vehicle.to_stop_id:
                            try:
                                # ベストマッチ検索
                                static_trip_id = trip_matcher.find_best_match(
                                    vehicle.trip_id,
                                    current_time_sec,
                                    vehicle.from_stop_id,
                                    vehicle.to_stop_id
                                )

                                if static_trip_id:
                                    # 位置補間
                                    position = trip_matcher.interpolate_position(
                                        static_trip_id,
                                        current_time_sec,
                                        vehicle.from_stop_id,
                                        vehicle.to_stop_id,
                                        0  # delay_sec（将来的にはvehicle.delayから取得）
                                    )

                                    if position:
                                        vehicle.lat = position["lat"]
                                        vehicle.lng = position["lng"]
                                        vehicle.progress = position["progress"]
                                        vehicle.interpolated = True
                                        interpolated_count += 1
                            except Exception as e:
                                # 個別のエラーはログだけ出して続行
                                pass

                    if interpolated_count > 0:
                        print(f"[poll_loop] GTFS interpolated {interpolated_count} vehicles")

                if v_merged:
                    with_pos = sum(1 for v in v_merged if v.lat is not None)
                    print(f"[poll_loop] {with_pos}/{len(v_merged)} vehicles have positions")

                key = normalize_railway_id(railway_filter) if railway_filter else "__ALL__"
                cache.vehicles_by_railway[key] = v_merged

                now = unix_ts()
                for v in v_merged:
                    cache.last_seen_by_trip[v.trip_id] = now
                    
        except Exception as e:
            print(f"[poll_loop] ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await asyncio.sleep(POLL_INTERVAL_SEC)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(poll_loop())

@app.get("/health")
async def health():
    return {"ok": True, "time": unix_ts(), "stations": len(cache.stations), "timetables": len(cache.timetables)}

@app.get("/api/stations")
async def api_stations(railwayId: Optional[str] = None):
    """駅リスト取得"""
    try:
        items = await fetch_odpt_stations(client, railwayId)
        out = []
        for it in items:
            out.append({
                "id": it.get("owl:sameAs"),
                "title": it.get("odpt:stationTitle", {}).get("ja") or it.get("dc:title"),
                "railwayId": normalize_railway_id(it.get("odpt:railway", "")),
                "lat": it.get("geo:lat"),
                "lng": it.get("geo:long") or it.get("geo:lon"),
            })
        return JSONResponse(out)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    
@app.get("/api/timetables")
async def get_timetables_api(railwayId: Optional[str] = None) -> Dict[str, Any]:
    """
    時刻表データをGTFS形式で返す
    返却形式: { trip_id: { stops: [{stop_id, arrival, departure, sequence}] } }
    """
    result = {}
    
    for trip_id, timetable in cache.timetables.items():
        # railwayIdが指定されている場合はフィルタ
        if railwayId:
            # trip_idに路線情報が含まれているか簡易チェック
            normalized_railway = normalize_railway_id(railwayId)
            if normalized_railway and normalized_railway not in trip_id:
                continue
        
        result[trip_id] = {
            "stops": timetable["stops"]
        }
    
    return result

@app.get("/api/trains/stream")
async def api_trains_stream(request: Request, railwayId: Optional[str] = None):
    """SSEでスナップショット配信"""
    railway_norm = normalize_railway_id(railwayId) if railwayId else None

    async def event_gen():
        last_sent_seq = -1
        last_hb = 0.0
        while True:
            if await request.is_disconnected():
                break
            snap = cache.snapshot(railway_norm)
            if snap.seq != last_sent_seq:
                data = snap.model_dump()
                yield f"event: snapshot\n" + f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                last_sent_seq = snap.seq
                last_hb = time.time()
            now = time.time()
            if now - last_hb >= SSE_HEARTBEAT_SEC:
                yield f"event: ping\n" + f"data: {unix_ts()}\n\n"
                last_hb = now
            await asyncio.sleep(0.2)

    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "text/event-stream",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_gen(), headers=headers, media_type="text/event-stream")
@app.get("/api/timetables")
async def get_timetables_api(railwayId: Optional[str] = None) -> Dict[str, Any]:
    """
    時刻表データをGTFS形式で返す
    返却形式: { trip_id: { stops: [{stop_id, arrival, departure, sequence}] } }
    """
    result = {}
    
    for trip_id, timetable in cache.timetables.items():
        # railwayIdが指定されている場合はフィルタ
        if railwayId:
            # trip_idに路線情報が含まれているか簡易チェック
            normalized_railway = normalize_railway_id(railwayId)
            if normalized_railway and normalized_railway not in trip_id:
                continue
        
        result[trip_id] = {
            "stops": timetable["stops"]
        }
    
    return result
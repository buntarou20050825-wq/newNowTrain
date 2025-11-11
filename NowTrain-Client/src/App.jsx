import React, { useState, useEffect, useRef, useCallback } from 'react';

const TrainMapJREastFiltered = () => {
  // ========== State管理 ==========
  const [stops, setStops] = useState([]);
  const [stopsMap, setStopsMap] = useState({});
  const [routes, setRoutes] = useState([]);
  const [trips, setTrips] = useState([]);
  const [stopTimes, setStopTimes] = useState([]);
  const [selectedOperator, setSelectedOperator] = useState('all');

  // ========== リアルタイムデータ管理 ==========
  const [realtimeMode, setRealtimeMode] = useState(false);
  const [liveConnected, setLiveConnected] = useState(false);
  const [serverUrl, setServerUrl] = useState('http://localhost:8000');
  const [realtimePositions, setRealtimePositions] = useState({});
  const [lastSeenByTrip, setLastSeenByTrip] = useState({});
  const [seqNum, setSeqNum] = useState(0);
  
  // ========== 補間アニメーション用 ==========
  const [timetablesCache, setTimetablesCache] = useState({});
  const [stationsCache, setStationsCache] = useState({}); // GTFS駅
  const [odptStationsCache, setOdptStationsCache] = useState({}); // ODPT駅
  const [displayPositions, setDisplayPositions] = useState({});
  const animationFrameRef = useRef(null);
  
  const canvasRef = useRef(null);
  const eventSourceRef = useRef(null);

  // ========== 事業者リスト ==========
  const operators = [
    { id: 'all', name: '全事業者', interpolation: false },
    { id: 'JR-East', name: 'JR東日本', interpolation: true },
    { id: 'Keikyu', name: '京急', interpolation: false },
    { id: 'Tobu', name: '東武', interpolation: false },
    { id: 'Odakyu', name: '小田急', interpolation: false }
  ];

  // ========== GTFSからの時刻表キャッシュ構築 ==========
  useEffect(() => {
    if (stopTimes.length === 0 || stops.length === 0) return;

    console.log('[Timetables] Building cache from GTFS stop_times (JR-East only)...');
    
    // stop_times.jsonをtrip_idでグループ化
    const timetables = {};
    stopTimes.forEach(st => {
      const tripId = st.trip_id;
      if (!timetables[tripId]) {
        timetables[tripId] = { stops: [] };
      }
      timetables[tripId].stops.push({
        stop_id: st.stop_id,
        arrival: st.arrival_time,
        departure: st.departure_time,
        sequence: parseInt(st.stop_sequence)
      });
    });

    // sequenceでソート
    Object.values(timetables).forEach(tt => {
      tt.stops.sort((a, b) => a.sequence - b.sequence);
    });

    setTimetablesCache(timetables);
    console.log('[Timetables] Built cache for', Object.keys(timetables).length, 'trips');

    // 駅座標キャッシュも構築
    const stationsMap = {};
    stops.forEach(stop => {
      stationsMap[stop.stop_id] = {
        lat: stop.stop_lat,
        lng: stop.stop_lon,
        name: stop.stop_name
      };
    });
    setStationsCache(stationsMap);
    console.log('[Stations] Cached', Object.keys(stationsMap).length, 'stations');
  }, [stopTimes, stops]);

  // ========== ODPT駅位置キャッシュをサーバーから取得 ==========
  const fetchOdptStations = useCallback(async (railwayId) => {
    try {
      console.log('[ODPT] Fetching stations for:', railwayId);
      const response = await fetch(`${serverUrl}/api/stations?railwayId=${railwayId}`);
      const data = await response.json();
      
      const odptMap = {};
      data.stations.forEach(station => {
        odptMap[station.id] = {
          lat: station.lat,
          lng: station.lon,
          name: station.name
        };
      });
      
      setOdptStationsCache(prev => ({ ...prev, ...odptMap }));
      console.log('[ODPT] Cached', Object.keys(odptMap).length, 'stations for', railwayId);
    } catch (error) {
      console.error('[ODPT] Failed to fetch stations:', error);
    }
  }, [serverUrl]);

  // ========== 時刻文字列を秒に変換（24時間超対応） ==========
  const timeToSeconds = (timeStr) => {
    if (!timeStr) return 0;
    const [h, m, s] = timeStr.split(':').map(Number);
    return h * 3600 + m * 60 + (s || 0);
  };

  // ========== 現在時刻を秒に変換（当日の経過秒数） ==========
  const getTimeOfDayInSeconds = (date) => {
    const hours = date.getHours();
    const minutes = date.getMinutes();
    const seconds = date.getSeconds();
    return hours * 3600 + minutes * 60 + seconds;
  };

  // ========== 距離計算（km） ==========
  const getDistance = (pos1, pos2) => {
    if (!pos1 || !pos2) return 0;
    const R = 6371;
    const dLat = (pos2.lat - pos1.lat) * Math.PI / 180;
    const dLng = (pos2.lng - pos1.lng) * Math.PI / 180;
    const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
              Math.cos(pos1.lat * Math.PI / 180) * Math.cos(pos2.lat * Math.PI / 180) *
              Math.sin(dLng / 2) * Math.sin(dLng / 2);
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    return R * c;
  };

  // ========== イージング関数 ==========
  const easeInOut = (t) => {
    return t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
  };

  // ========== trip_id正規化（ODPTとGTFSの突合せ用） ==========
  const normalizeTripId = (tripId) => {
    if (!tripId) return '';
    const parts = tripId.split('.');
    if (parts.length > 0 && parts[parts.length - 1].match(/^\d{8}$/)) {
      parts.pop();
    }
    return parts[parts.length - 1] || tripId;
  };

  // ========== 事業者判定 ==========
  const getOperator = (tripId) => {
    if (!tripId) return null;
    if (tripId.startsWith('JR-East.')) return 'JR-East';
    if (tripId.startsWith('Keikyu.')) return 'Keikyu';
    if (tripId.startsWith('Tobu.')) return 'Tobu';
    if (tripId.startsWith('Odakyu.')) return 'Odakyu';
    return 'other';
  };

  // ========== 暫定補間：駅間の線形補間（時刻表なしでも動く） ==========
  const calculateSimpleInterpolation = useCallback((tripId, now) => {
    const realtime = realtimePositions[tripId];
    if (!realtime || !realtime.from_stop_id || !realtime.to_stop_id) {
      return null;
    }

    // 停車中
    if (realtime.to_stop_id === 'null' || !realtime.to_stop_id) {
      const fromPos = odptStationsCache[realtime.from_stop_id];
      if (fromPos) {
        return {
          lat: fromPos.lat,
          lng: fromPos.lng,
          progress: 0,
          status: 'stopped'
        };
      }
    }

    // 駅位置を取得
    const fromPos = odptStationsCache[realtime.from_stop_id];
    const toPos = odptStationsCache[realtime.to_stop_id];

    if (!fromPos || !toPos) {
      return null;
    }

    // 遅延を考慮した進捗率（0.5を基準に±調整）
    const delay = realtime.delay || 0;
    const baseProgress = 0.5; // デフォルト中間
    
    // 3秒周期で進捗を更新（サーバーのポーリング間隔）
    const elapsedSinceUpdate = (Date.now() - (lastSeenByTrip[tripId] || Date.now())) / 1000;
    const interpolatedProgress = Math.min(1, baseProgress + (elapsedSinceUpdate / 6)); // 3秒で0.5進む想定

    // 線形補間
    return {
      lat: fromPos.lat + (toPos.lat - fromPos.lat) * interpolatedProgress,
      lng: fromPos.lng + (toPos.lng - fromPos.lng) * interpolatedProgress,
      progress: interpolatedProgress,
      status: 'moving'
    };
  }, [realtimePositions, odptStationsCache, lastSeenByTrip]);

  // ========== 完璧な時刻表マッチング ==========
  const findBestTimetable = useCallback(({ rtTripId, nowSec, fromStopId, toStopId }) => {
    // 列車番号抽出（例: "JR-East.Chuo.554M" → "554M"）
    const trainNumber = rtTripId.split('.').pop();

    // 候補抽出（trip_id に "554M" を含むもの）
    const candidateIds = Object.keys(timetablesCache).filter(id => id.includes(trainNumber));
    if (candidateIds.length === 0) return null;

    let best = null;
    let bestScore = -Infinity;

    for (const cid of candidateIds) {
      const tt = timetablesCache[cid];
      if (!tt || !tt.stops || tt.stops.length < 2) continue;

      const firstDep = timeToSeconds(tt.stops[0].departure || tt.stops[0].arrival || "00:00:00");
      const timeDiff = Math.abs(nowSec - firstDep);

      // 駅マッチ（ODPT ↔ GTFS）
      const idxFrom = tt.stops.findIndex(s => matchStopIdImproved(s.stop_id, fromStopId));
      const idxTo = tt.stops.findIndex(s => matchStopIdImproved(s.stop_id, toStopId));
      const hasFrom = idxFrom >= 0;
      const hasTo = idxTo >= 0;

      let score = -timeDiff;
      if (hasFrom) score += 10000;
      if (hasTo) score += 10000;
      if (hasFrom && hasTo && idxFrom < idxTo) score += 1000;

      // 現在時刻が区間内にあるか
      if (hasFrom && hasTo && idxFrom < idxTo) {
        const dep = timeToSeconds(tt.stops[idxFrom].departure || tt.stops[idxFrom].arrival);
        const arr = timeToSeconds(tt.stops[idxTo].arrival || tt.stops[idxTo].departure);
        if (dep < arr && nowSec >= dep && nowSec <= arr) {
          score += 500;
        }
      }

      if (score > bestScore) {
        bestScore = score;
        best = { id: cid, timetable: tt };
      }
    }

    return best;
  }, [timetablesCache]);

  // ========== stop_idマッチング改善版（座標ベース） ==========
  const matchStopIdImproved = (gtfsStopId, odptStopId) => {
    if (!gtfsStopId || !odptStopId) return false;
    if (gtfsStopId === odptStopId) return true;

    // GTFS駅座標
    const gtfsStation = stationsCache[gtfsStopId];
    // ODPT駅座標
    const odptStation = odptStationsCache[odptStopId];

    if (gtfsStation && odptStation) {
      // 座標で距離計算（300m以内なら一致）
      const distance = getDistance(gtfsStation, odptStation);
      if (distance < 0.3) { // 300m
        return true;
      }
    }

    // フォールバック：名称マッチ
    if (gtfsStation && odptStopId.includes(gtfsStation.name)) {
      return true;
    }

    return false;
  };

  // ========== 時刻表ベースの位置計算（完全版） ==========
  const calculateTimetablePosition = useCallback((tripId, now) => {
    // JR東日本以外は暫定補間
    if (!tripId.startsWith('JR-East.')) {
      return calculateSimpleInterpolation(tripId, now);
    }

    const realtime = realtimePositions[tripId];
    if (!realtime || !realtime.from_stop_id) {
      return calculateSimpleInterpolation(tripId, now);
    }

    const nowSec = getTimeOfDayInSeconds(now);
    const delay = realtime.delay || 0;

    // ベスト時刻表を検索
    const bestMatch = findBestTimetable({
      rtTripId: tripId,
      nowSec: nowSec,
      fromStopId: realtime.from_stop_id,
      toStopId: realtime.to_stop_id
    });

    if (!bestMatch) {
      // 時刻表が見つからない場合は暫定補間
      return calculateSimpleInterpolation(tripId, now);
    }

    const timetable = bestMatch.timetable;
    const fromStopId = realtime.from_stop_id;
    const toStopId = realtime.to_stop_id;

    // 停車中
    if (!toStopId || toStopId === 'null') {
      const fromPos = odptStationsCache[fromStopId];
      if (fromPos) {
        return { lat: fromPos.lat, lng: fromPos.lng, progress: 0, status: 'stopped' };
      }
    }

    // 時刻表から駅情報取得
    const stops = timetable.stops || [];
    const fromStop = stops.find(s => matchStopIdImproved(s.stop_id, fromStopId));
    const toStop = stops.find(s => matchStopIdImproved(s.stop_id, toStopId));

    if (!fromStop || !toStop) {
      return calculateSimpleInterpolation(tripId, now);
    }

    // GTFS駅座標
    const fromPosGtfs = stationsCache[fromStop.stop_id];
    const toPosGtfs = stationsCache[toStop.stop_id];

    // ODPT駅座標（フォールバック）
    const fromPosOdpt = odptStationsCache[fromStopId];
    const toPosOdpt = odptStationsCache[toStopId];

    const fromPos = fromPosGtfs || fromPosOdpt;
    const toPos = toPosGtfs || toPosOdpt;

    if (!fromPos || !toPos) {
      return calculateSimpleInterpolation(tripId, now);
    }

    // 時刻計算
    const depTime = timeToSeconds(fromStop.departure || fromStop.arrival) + delay;
    const arrTime = timeToSeconds(toStop.arrival || toStop.departure) + delay;
    const currentTime = nowSec;

    const duration = arrTime - depTime;
    if (duration <= 0) {
      return { lat: fromPos.lat, lng: fromPos.lng, progress: 0, status: 'stopped' };
    }

    const progress = (currentTime - depTime) / duration;
    const clampedProgress = Math.max(0, Math.min(1, progress));

    // 線形補間
    return {
      lat: fromPos.lat + (toPos.lat - fromPos.lat) * clampedProgress,
      lng: fromPos.lng + (toPos.lng - fromPos.lng) * clampedProgress,
      progress: clampedProgress,
      status: 'moving'
    };
  }, [timetablesCache, realtimePositions, stationsCache, odptStationsCache, findBestTimetable, calculateSimpleInterpolation]);

  // ========== stop_id のマッチング ==========
  const matchStopId = (gtfsStopId, odptStopId) => {
    if (!gtfsStopId || !odptStopId) return false;
    if (gtfsStopId === odptStopId) return true;
    const gtfsStation = stationsCache[gtfsStopId];
    if (gtfsStation && odptStopId.includes(gtfsStation.name)) {
      return true;
    }
    return false;
  };

  // ========== 駅位置を検索 ==========
  const findStationPos = (odptStationId) => {
    if (!odptStationId) return null;
    const parts = odptStationId.split('.');
    const stationName = parts[parts.length - 1];
    const matchingStation = stops.find(stop => 
      stop.stop_name && (
        stop.stop_name.includes(stationName) ||
        stop.stop_name.replace(/\s/g, '').toLowerCase().includes(stationName.toLowerCase())
      )
    );
    if (matchingStation) {
      return {
        lat: matchingStation.stop_lat,
        lng: matchingStation.stop_lon,
        name: matchingStation.stop_name
      };
    }
    return null;
  };

  // ========== GTFS-RTスナップショット受信時の処理 ==========
  const onSnapshotReceived = useCallback((snapshot) => {
    const now = Date.now();
    const newRealtimePositions = {};
    const newLastSeen = { ...lastSeenByTrip };

    snapshot.vehicles.forEach(vehicle => {
      const tripId = vehicle.trip_id;
      const operator = getOperator(tripId);
      
      newRealtimePositions[tripId] = vehicle;
      newLastSeen[tripId] = now;

      setDisplayPositions(prev => {
        const current = prev[tripId];
        
        if (vehicle.lat && vehicle.lng) {
          // JR東日本以外、または時刻表がない場合はGTFS-RT座標をそのまま使用
          if (operator !== 'JR-East') {
            return {
              ...prev,
              [tripId]: {
                lat: vehicle.lat,
                lng: vehicle.lng,
                mode: "realtime-only",
                operator: operator
              }
            };
          }

          // JR東日本の場合、時刻表ベース補間と比較
          if (current && current.mode === "timetable") {
            const distance = getDistance(current, vehicle);
            
            // 2km以上ずれていたら補正（しきい値を大幅に上げた）
            if (distance > 2.0) {
              console.log(`[${tripId}] Large correction needed: ${(distance * 1000).toFixed(0)}m`);
              return {
                ...prev,
                [tripId]: {
                  lat: current.lat,
                  lng: current.lng,
                  mode: "correcting",
                  correctionStart: now,
                  correctionFrom: { lat: current.lat, lng: current.lng },
                  correctionTo: { lat: vehicle.lat, lng: vehicle.lng },
                  operator: operator
                }
              };
            }
          } else {
            return {
              ...prev,
              [tripId]: {
                lat: vehicle.lat,
                lng: vehicle.lng,
                mode: "timetable",
                operator: operator
              }
            };
          }
        } else {
          if (!current) {
            return {
              ...prev,
              [tripId]: {
                lat: null,
                lng: null,
                mode: "timetable",
                operator: operator
              }
            };
          }
        }
        
        return prev;
      });
    });

    setRealtimePositions(newRealtimePositions);
    setLastSeenByTrip(newLastSeen);
    setSeqNum(snapshot.seq || 0);
  }, [lastSeenByTrip]);

  // ========== SSE接続 ==========
  const connectSSE = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    const url = `${serverUrl}/api/trains/stream`;
    console.log('[SSE] Connecting to:', url);
    
    const es = new EventSource(url);
    
    es.addEventListener('snapshot', (e) => {
      try {
        const data = JSON.parse(e.data);
        onSnapshotReceived(data);
      } catch (err) {
        console.error('[SSE] Parse error:', err);
      }
    });

    es.addEventListener('ping', (e) => {
      // ハートビート受信
    });

    es.onopen = () => {
      console.log('[SSE] Connected');
      setLiveConnected(true);
      setRealtimeMode(true);
    };

    es.onerror = () => {
      console.error('[SSE] Connection error');
      setLiveConnected(false);
      es.close();
      
      setTimeout(() => {
        if (realtimeMode) {
          console.log('[SSE] Reconnecting...');
          connectSSE();
        }
      }, 5000);
    };

    eventSourceRef.current = es;
  }, [serverUrl, realtimeMode, onSnapshotReceived]);

  // ========== LIVE接続トグル ==========
  const toggleLiveConnection = () => {
    if (realtimeMode) {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      setRealtimeMode(false);
      setLiveConnected(false);
      setRealtimePositions({});
      setDisplayPositions({});
    } else {
      if (stopTimes.length === 0) {
        alert('先にGTFSファイル（特にstop_times.json）を読み込んでください！');
        return;
      }
      
      // 主要路線のODPT駅情報を取得
      const mainRailways = [
        'odpt.Railway:JR-East.ChuoRapid',
        'odpt.Railway:JR-East.Chuo',
        'odpt.Railway:JR-East.Yamanote',
        'odpt.Railway:JR-East.Joban',
        'odpt.Railway:JR-East.Keihin'
      ];
      
      mainRailways.forEach(railwayId => fetchOdptStations(railwayId));
      
      connectSSE();
    }
  };

  // ========== 60fps描画ループ ==========
  const animate = useCallback(() => {
    const now = Date.now();
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const rect = canvas.getBoundingClientRect();
    
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, rect.width, rect.height);

    let minLat = 90, maxLat = -90, minLng = 180, maxLng = -180;
    let hasPositions = false;

    const updatedDisplayPositions = {};

    Object.entries(displayPositions).forEach(([tripId, display]) => {
      // 事業者フィルタ
      if (selectedOperator !== 'all' && display.operator !== selectedOperator) {
        return;
      }

      let drawPos = null;

      if (display.mode === "correcting") {
        // 補正中（1秒かけてスムーズに移動）
        const elapsed = now - display.correctionStart;
        const t = Math.min(1, elapsed / 1000);
        
        drawPos = {
          lat: display.correctionFrom.lat + 
               (display.correctionTo.lat - display.correctionFrom.lat) * easeInOut(t),
          lng: display.correctionFrom.lng + 
               (display.correctionTo.lng - display.correctionFrom.lng) * easeInOut(t)
        };

        updatedDisplayPositions[tripId] = {
          ...display,
          lat: drawPos.lat,
          lng: drawPos.lng
        };

        if (t >= 1) {
          updatedDisplayPositions[tripId].mode = "timetable";
        }
      } else if (display.mode === "realtime-only") {
        // JR東日本以外：GTFS-RT座標のみ
        const rtPos = realtimePositions[tripId];
        if (rtPos && rtPos.lat && rtPos.lng) {
          drawPos = { lat: rtPos.lat, lng: rtPos.lng };
          updatedDisplayPositions[tripId] = {
            ...display,
            lat: drawPos.lat,
            lng: drawPos.lng
          };
        }
      } else {
        // JR東日本：時刻表ベース補間
        const calculatedPos = calculateTimetablePosition(tripId, new Date());
        if (calculatedPos) {
          drawPos = calculatedPos;
          updatedDisplayPositions[tripId] = {
            lat: drawPos.lat,
            lng: drawPos.lng,
            mode: "timetable",
            operator: display.operator
          };
        } else if (display.lat && display.lng) {
          drawPos = display;
          updatedDisplayPositions[tripId] = display;
        }
      }

      if (drawPos && drawPos.lat && drawPos.lng) {
        hasPositions = true;
        minLat = Math.min(minLat, drawPos.lat);
        maxLat = Math.max(maxLat, drawPos.lat);
        minLng = Math.min(minLng, drawPos.lng);
        maxLng = Math.max(maxLng, drawPos.lng);
      }
    });

    setDisplayPositions(updatedDisplayPositions);

    if (!hasPositions) {
      ctx.fillStyle = '#666';
      ctx.font = '14px sans-serif';
      ctx.fillText('列車データを取得中...', rect.width / 2 - 70, rect.height / 2);
      animationFrameRef.current = requestAnimationFrame(animate);
      return;
    }

    const latMargin = (maxLat - minLat) * 0.1 || 0.1;
    const lngMargin = (maxLng - minLng) * 0.1 || 0.1;
    minLat -= latMargin;
    maxLat += latMargin;
    minLng -= lngMargin;
    maxLng += lngMargin;

    const latToY = (lat) => {
      return rect.height - ((lat - minLat) / (maxLat - minLat)) * rect.height;
    };
    const lngToX = (lng) => {
      return ((lng - minLng) / (maxLng - minLng)) * rect.width;
    };

    // 列車を描画
    Object.entries(updatedDisplayPositions).forEach(([tripId, display]) => {
      if (!display.lat || !display.lng) return;
      if (selectedOperator !== 'all' && display.operator !== selectedOperator) return;

      const x = lngToX(display.lng);
      const y = latToY(display.lat);

      const lastSeen = lastSeenByTrip[tripId];
      const age = lastSeen ? (now - lastSeen) / 1000 : 999;
      if (age > 15) return;

      const freshness = Math.max(0, 1 - age / 15);

      const realtime = realtimePositions[tripId];
      const delay = realtime?.delay || 0;
      
      // 事業者ごとの色分け
      let color = '#3B82F6'; // デフォルト青
      if (display.operator === 'JR-East') {
        color = delay >= 300 ? '#EF4444' : delay >= 60 ? '#F59E0B' : '#10B981'; // 緑（JR東日本）
      } else if (display.operator === 'Keikyu') {
        color = '#EF4444'; // 赤（京急）
      } else if (display.operator === 'Tobu') {
        color = '#3B82F6'; // 青（東武）
      } else if (display.operator === 'Odakyu') {
        color = '#0EA5E9'; // 水色（小田急）
      }

      // 波紋エフェクト
      if (age < 3) {
        const rippleProgress = (age % 1);
        const rippleRadius = 15 + rippleProgress * 10;
        ctx.beginPath();
        ctx.arc(x, y, rippleRadius, 0, Math.PI * 2);
        ctx.strokeStyle = `${color}${Math.floor((1 - rippleProgress) * 0.5 * 255).toString(16).padStart(2, '0')}`;
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      // 列車本体
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fillStyle = `${color}${Math.floor(freshness * 255).toString(16).padStart(2, '0')}`;
      ctx.fill();
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 2;
      ctx.stroke();

      // JR東日本で時刻表補間中の場合、小さいマーカー
      if (display.mode === 'timetable' && display.operator === 'JR-East') {
        ctx.fillStyle = '#fff';
        ctx.beginPath();
        ctx.arc(x, y, 2, 0, Math.PI * 2);
        ctx.fill();
      }
    });

    animationFrameRef.current = requestAnimationFrame(animate);
  }, [displayPositions, realtimePositions, lastSeenByTrip, calculateTimetablePosition, selectedOperator]);

  // ========== アニメーション開始 ==========
  useEffect(() => {
    if (realtimeMode && liveConnected) {
      animationFrameRef.current = requestAnimationFrame(animate);
      return () => {
        if (animationFrameRef.current) {
          cancelAnimationFrame(animationFrameRef.current);
        }
      };
    }
  }, [realtimeMode, liveConnected, animate]);

  // ========== クリーンアップ ==========
  useEffect(() => {
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
      if (animationFrameRef.current) {
        cancelAnimationFrame(animationFrameRef.current);
      }
    };
  }, []);

  // ========== GTFSファイル読み込み ==========
  const handleFileUpload = async (event) => {
    const files = Array.from(event.target.files);
    
    for (const file of files) {
      const text = await file.text();
      const data = JSON.parse(text);
      
      if (file.name.includes('stops')) {
        setStops(data);
        const map = {};
        data.forEach(stop => {
          map[stop.stop_id] = stop;
        });
        setStopsMap(map);
        console.log('[GTFS] Loaded', data.length, 'stops');
      } else if (file.name.includes('routes')) {
        setRoutes(data);
        console.log('[GTFS] Loaded', data.length, 'routes');
      } else if (file.name.includes('trips')) {
        setTrips(data);
        console.log('[GTFS] Loaded', data.length, 'trips');
      } else if (file.name.includes('stop_times')) {
        setStopTimes(data);
        console.log('[GTFS] Loaded', data.length, 'stop_times');
      }
    }
  };

  // ========== デバッグ用 ==========
  useEffect(() => {
    window.DEBUG = {
      timetablesCache,
      stationsCache,
      displayPositions,
      realtimePositions,
      stopTimes,
      operators: Object.keys(displayPositions).reduce((acc, tripId) => {
        const op = displayPositions[tripId].operator;
        acc[op] = (acc[op] || 0) + 1;
        return acc;
      }, {})
    };
  }, [timetablesCache, stationsCache, displayPositions, realtimePositions, stopTimes]);

  // 事業者ごとの統計
  const operatorStats = Object.keys(displayPositions).reduce((acc, tripId) => {
    const op = displayPositions[tripId]?.operator || 'unknown';
    acc[op] = (acc[op] || 0) + 1;
    return acc;
  }, {});

  // ========== レンダリング ==========
  return (
    <div style={{ width: '100%', height: '100vh', background: '#1a1a1a', color: '#fff', fontFamily: 'sans-serif' }}>
      {/* ヘッダー */}
      <div style={{ padding: '15px', background: '#2a2a2a', borderBottom: '2px solid #3a3a3a' }}>
        <h1 style={{ margin: '0 0 15px 0', fontSize: '24px' }}>JR東日本 リアルタイム電車マップ（完全版）</h1>
        
        <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'center' }}>
          <button
            onClick={() => document.getElementById('gtfs-upload').click()}
            style={{
              padding: '8px 16px',
              background: '#4B5563',
              border: 'none',
              borderRadius: '4px',
              color: '#fff',
              cursor: 'pointer'
            }}
          >
            📤 GTFS読込
          </button>
          <input
            id="gtfs-upload"
            type="file"
            multiple
            accept=".json"
            onChange={handleFileUpload}
            style={{ display: 'none' }}
          />

          <select
            value={selectedOperator}
            onChange={(e) => setSelectedOperator(e.target.value)}
            style={{
              padding: '8px',
              background: '#374151',
              border: '1px solid #4B5563',
              borderRadius: '4px',
              color: '#fff'
            }}
          >
            {operators.map(op => (
              <option key={op.id} value={op.id}>
                {op.name} {op.interpolation ? '(補間あり)' : ''}
              </option>
            ))}
          </select>

          <div style={{ 
            padding: '8px 12px',
            background: '#374151',
            borderRadius: '4px',
            fontSize: '12px'
          }}>
            時刻表: {Object.keys(timetablesCache).length}件 | 駅: {Object.keys(stationsCache).length}件
          </div>

          <input
            type="text"
            value={serverUrl}
            onChange={(e) => setServerUrl(e.target.value)}
            placeholder="http://localhost:8000"
            style={{
              padding: '8px',
              background: '#374151',
              border: '1px solid #4B5563',
              borderRadius: '4px',
              color: '#fff',
              width: '200px'
            }}
          />

          <button
            onClick={toggleLiveConnection}
            disabled={stopTimes.length === 0}
            style={{
              padding: '8px 16px',
              background: stopTimes.length === 0 ? '#6B7280' : (realtimeMode ? '#EF4444' : '#10B981'),
              border: 'none',
              borderRadius: '4px',
              color: '#fff',
              cursor: stopTimes.length === 0 ? 'not-allowed' : 'pointer',
              fontWeight: 'bold'
            }}
          >
            {realtimeMode ? '📡 切断' : '📡 LIVE接続'}
          </button>

          <div style={{ 
            padding: '8px 16px',
            background: liveConnected ? '#10B981' : '#6B7280',
            borderRadius: '4px',
            fontSize: '14px'
          }}>
            {liveConnected ? `✓ LIVE: ${Object.keys(displayPositions).length}編成 (seq:${seqNum})` : '✗ オフライン'}
          </div>
        </div>

        {/* 事業者ごとの統計 */}
        <div style={{ marginTop: '10px', display: 'flex', gap: '15px', fontSize: '12px' }}>
          {Object.entries(operatorStats).map(([op, count]) => (
            <div key={op} style={{ 
              padding: '4px 8px', 
              background: '#374151', 
              borderRadius: '4px' 
            }}>
              {op === 'JR-East' && '🟢'} 
              {op === 'Keikyu' && '🔴'} 
              {op === 'Tobu' && '🔵'} 
              {op === 'Odakyu' && '🔵'}
              {op}: {count}編成
            </div>
          ))}
        </div>
      </div>

      {/* 地図キャンバス */}
      <canvas
        ref={canvasRef}
        style={{
          width: '100%',
          height: 'calc(100vh - 140px)',
          background: '#f5f5f5',
          display: 'block'
        }}
      />
    </div>
  );
};

export default TrainMapJREastFiltered;

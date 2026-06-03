import httpx
import os
import math
from fastmcp import FastMCP
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("商业地图服务集成平台")
AMAP_KEY = os.getenv("AMAP_API_KEY")
BASE_URL = "https://restapi.amap.com/v3"


# ========== 基础工具 ==========

@mcp.tool()
async def geocode(address: str) -> dict:
    """将地址转换为经纬度坐标。输入中文地址，返回经纬度和格式化地址。"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/geocode/geo", params={
            "key": AMAP_KEY, "address": address, "output": "JSON"
        })
        data = resp.json()
        if data["status"] == "1" and data["geocodes"]:
            loc = data["geocodes"][0]
            lng, lat = loc["location"].split(",")
            return {
                "address": loc["formatted_address"],
                "longitude": float(lng), "latitude": float(lat),
                "city": loc.get("city", ""), "adcode": loc.get("adcode", "")
            }
        return {"error": "地理编码失败", "detail": data}


@mcp.tool()
async def reverse_geocode(longitude: float, latitude: float) -> dict:
    """将经纬度坐标转换为详细地址。"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/geocode/regeo", params={
            "key": AMAP_KEY,
            "location": f"{longitude},{latitude}", "output": "JSON",
            "extensions": "all"
        })
        data = resp.json()
        if data["status"] == "1":
            info = data["regeocode"]
            ac = info["addressComponent"]
            aois = [a["name"] for a in info.get("aois", [])[:3]]
            return {
                "formatted_address": info["formatted_address"],
                "province": ac.get("province", ""),
                "city": ac.get("city", ""), "district": ac.get("district", ""),
                "nearby_pois": aois,
            }
        return {"error": "逆地理编码失败"}


async def _resolve_location(address: str) -> tuple:
    """内部：地址→(坐标字符串, geo_dict) 或 (None, error_dict)"""
    geo = await geocode(address)
    if "error" in geo:
        return None, geo
    return f"{geo['longitude']},{geo['latitude']}", geo


# ========== 路径规划（4种交通方式）==========

@mcp.tool()
async def route_driving(origin: str, destination: str) -> dict:
    """规划驾车路线。返回距离、时间、过路费、路线折线坐标、路段指引、红绿灯数。"""
    o_loc, o_geo = await _resolve_location(origin)
    d_loc, d_geo = await _resolve_location(destination)
    if not o_loc or not d_loc:
        return {"error": "地址解析失败", "origin_result": o_geo, "dest_result": d_geo}

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/direction/driving", params={
            "key": AMAP_KEY, "origin": o_loc,
            "destination": d_loc, "strategy": 0,
            "output": "JSON", "extensions": "all"
        })
        data = resp.json()
        if data["status"] == "1":
            route = data["route"]["paths"][0]
            all_points, steps_desc = [], []
            for step in route.get("steps", []):
                steps_desc.append({"instruction": step.get("instruction", ""),
                    "road": step.get("road", ""),
                    "distance_m": step.get("distance", 0),
                    "duration_s": step.get("duration", 0)})
                if step.get("polyline"):
                    all_points.extend(decode_polyline(step["polyline"]))
            dist_km = round(int(route["distance"]) / 1000, 1)
            dur_min = round(int(route["duration"]) / 60, 1)
            return {
                "mode": "驾车",
                "origin": o_geo.get("address", origin),
                "destination": d_geo.get("address", destination),
                "origin_coords": [o_geo["longitude"], o_geo["latitude"]],
                "dest_coords": [d_geo["longitude"], d_geo["latitude"]],
                "distance_km": dist_km,
                "duration_min": dur_min,
                "toll_fee_yuan": round(float(route.get("tolls", "0")) / 100, 2),
                "toll_distance_km": round(int(route.get("toll_distance", 0)) / 1000, 2),
                "route_polyline": all_points[:500],
                "steps": steps_desc[:10],
                "traffic_lights": int(route.get("traffic_lights", 0)),
                "summary": f"驾车约{dist_km}公里，最快约{dur_min}分钟",
                "status": "success"
            }
        return {"error": "驾车路径规划失败", "detail": data.get("info", data), "status": "failed"}


@mcp.tool()
async def route_transit(origin: str, destination: str, city: str = "") -> dict:
    """规划公交/地铁换乘路线。返回多个方案含票价、步行距离、每段详情。"""
    o_loc, o_geo = await _resolve_location(origin)
    d_loc, d_geo = await _resolve_location(destination)
    if not o_loc or not d_loc:
        return {"error": "地址解析失败，请确认地址名称正确", "status": "failed"}

    use_city = city or o_geo.get("city", "")

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/direction/transit/integrated", params={
            "key": AMAP_KEY, "origin": o_loc,
            "destination": d_loc, "city": use_city,
            "strategy": 0, "output": "JSON"
        })
        data = resp.json()
        if data["status"] != "1":
            return {"error": f"公交查询失败: {data.get('info', '未知错误')}", "status": "failed"}
        if not data.get("route") or not data["route"].get("transits"):
            return {"error": "未找到公交路线", "status": "empty"}

        transits = data["route"]["transits"]
        results = []
        for idx, tr in enumerate(transits[:3]):
            segments = []
            total_walk_dist = 0
            for seg in tr.get("segments", []):
                if seg.get("walking"):
                    walk = seg["walking"]
                    wdist = int(walk.get("distance", 0))
                    segments.append({"type": "步行", "distance_m": wdist,
                        "duration_s": int(walk.get("duration", 0)),
                        "guide": walk.get("guide", "")})
                    total_walk_dist += wdist
                elif seg.get("bus") or seg.get("railway"):
                    bus = seg.get("bus") or seg.get("railway")
                    lines = bus.get("lines", [{}])
                    line = lines[0] if lines else {}
                    seg_type = "地铁" if bus.get("railway") else "公交"
                    segments.append({
                        "type": seg_type,
                        "line_name": line.get("name", ""),
                        "departure_stop": line.get("departure_stop", {}).get("name", ""),
                        "arrival_stop": line.get("arrival_stop", {}).get("name", ""),
                        "via_count": len(line.get("via_stops", []))})

            dur_s = int(tr.get("duration", 0))
            dur_h, dur_m = dur_s // 3600, (dur_s % 3600) // 60
            time_str = f"约{dur_h}小时{dur_m}分钟" if dur_h > 0 else f"约{dur_m}分钟"
            cost = float(tr.get("cost", 0))

            results.append({
                "scheme_id": idx + 1, "total_duration": time_str,
                "total_seconds": dur_s,
                "walking_distance_km": round(total_walk_dist / 1000, 1),
                "cost_yuan": cost, "segments": segments,
                "summary": f"方案{idx+1}: {time_str}, 步行{round(total_walk_dist/1000,1)}km, 票价{cost}元"})

        return {
            "mode": "公交/地铁",
            "origin": o_geo.get("address", origin),
            "destination": d_geo.get("address", destination),
            "best_scheme": results[0],
            "alternative_schemes": results[1:],
            "total_alternatives": len(results), "city": use_city,
            "status": "success"}


@mcp.tool()
async def route_bicycling(origin: str, destination: str) -> dict:
    """规划骑行路线。若高德服务不可用则自动降级估算。"""
    o_loc, o_geo = await _resolve_location(origin)
    d_loc, d_geo = await _resolve_location(destination)
    if not o_loc or not d_loc:
        return {"error": "地址解析失败", "status": "failed"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/direction/bicycling", params={
            "key": AMAP_KEY, "origin": o_loc,
            "destination": d_loc, "output": "JSON"})
        data = resp.json()

        if data["status"] != "1":
            # 降级：用驾车距离估算
            try:
                r2 = await client.get(f"{BASE_URL}/direction/driving", params={
                    "key": AMAP_KEY, "origin": o_loc, "destination": d_loc, "output": "JSON"})
                d2 = r2.json()
                drive_dist = int(d2["route"]["paths"][0]["distance"]) if d2["status"] == "1" else None
            except Exception:
                drive_dist = None

            if drive_dist:
                dist_km = round(drive_dist / 1000, 1)
            else:
                o_lng, o_lat = float(o_loc.split(",")[0]), float(o_loc.split(",")[1])
                d_lng, d_lat = float(d_loc.split(",")[0]), float(d_loc.split(",")[1])
                straight = math.sqrt((d_lng - o_lng)**2 + (d_lat - o_lat)**2) * 111000
                dist_km = round(straight * 1.3 / 1000, 1)

            dur_min = round(dist_km / 15 * 60, 0)
            return {
                "mode": "骑行（估算）",
                "origin": o_geo.get("address", origin),
                "destination": d_geo.get("address", destination),
                "origin_coords": [o_geo["longitude"], o_geo["latitude"]],
                "dest_coords": [d_geo["longitude"], d_geo["latitude"]],
                "distance_km": dist_km, "duration_min": dur_min,
                "note": "高德骑行服务暂不可用，已根据道路距离估算（平均15km/h）",
                "summary": f"骑行约{dist_km}公里，估算{int(dur_min)}分钟",
                "status": "estimated"}

        route = data["route"]["paths"][0]
        all_points = []
        for step in route.get("steps", []):
            if step.get("polyline"):
                all_points.extend(decode_polyline(step["polyline"]))
        dist_km = round(int(route["distance"]) / 1000, 1)
        dur_min = round(int(route["duration"]) / 60, 1)
        return {
            "mode": "骑行",
            "origin": o_geo.get("address", origin),
            "destination": d_geo.get("address", destination),
            "origin_coords": [o_geo["longitude"], o_geo["latitude"]],
            "dest_coords": [d_geo["longitude"], d_geo["latitude"]],
            "distance_km": dist_km, "duration_min": dur_min,
            "route_polyline": all_points[:500],
            "summary": f"骑行约{dist_km}公里，约{dur_min}分钟",
            "status": "success"}


@mcp.tool()
async def route_walking(origin: str, destination: str) -> dict:
    """规划步行路线。返回距离、时间和路线坐标折线。"""
    o_loc, o_geo = await _resolve_location(origin)
    d_loc, d_geo = await _resolve_location(destination)
    if not o_loc or not d_loc:
        return {"error": "地址解析失败", "status": "failed"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/direction/walking", params={
            "key": AMAP_KEY, "origin": o_loc,
            "destination": d_loc, "output": "JSON"})
        data = resp.json()
        if data["status"] == "1":
            route = data["route"]["paths"][0]
            all_points = []
            for step in route.get("steps", []):
                if step.get("polyline"):
                    all_points.extend(decode_polyline(step["polyline"]))
            dist_km = round(int(route["distance"]) / 1000, 1)
            dur_min = round(int(route["duration"]) / 60, 1)
            return {
                "mode": "步行",
                "origin": o_geo.get("address", origin),
                "destination": d_geo.get("address", destination),
                "origin_coords": [o_geo["longitude"], o_geo["latitude"]],
                "dest_coords": [d_geo["longitude"], d_geo["latitude"]],
                "distance_km": dist_km, "duration_min": dur_min,
                "route_polyline": all_points[:500],
                "summary": f"步行约{dist_km}公里，约{int(dur_min)}分钟",
                "status": "success"}
        return {"error": f"步行路径规划失败: {data.get('info','')}", "status": "failed"}


# ========== POI 搜索 ==========

@mcp.tool()
async def poi_search(keywords: str, city: str, poi_type: str = "") -> dict:
    """搜索指定城市的兴趣点。"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/place/text", params={
            "key": AMAP_KEY, "keywords": keywords, "city": city,
            "types": poi_type, "output": "JSON", "offset": 10, "page": 1})
        data = resp.json()
        if data["status"] == "1":
            results = [{"name": p["name"], "address": p["address"],
                        "location": p["location"], "tel": p.get("tel", "无")}
                       for p in data["pois"][:5]]
            return {"count": data["count"], "pois": results}
        return {"error": "POI搜索失败"}


@mcp.tool()
async def nearby_search(longitude: float, latitude: float, keywords: str,
                        radius: int = 1000) -> dict:
    """搜索某坐标周边的设施。"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/place/around", params={
            "key": AMAP_KEY, "location": f"{longitude},{latitude}",
            "keywords": keywords, "radius": radius,
            "output": "JSON", "offset": 8})
        data = resp.json()
        if data["status"] == "1":
            results = [{"name": p["name"], "distance_m": p.get("distance", "?"),
                        "address": p["address"], "location": p.get("location", "")}
                       for p in data["pois"][:8]]
            return {"count": data["count"], "center": [longitude, latitude],
                    "nearby": results, "radius_m": radius}
        return {"error": "周边搜索失败"}


# ========== REST API 接口 ==========

api = FastAPI(title="地图MCP服务")
api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
api.mount("/frontend", StaticFiles(directory="frontend", html=True), name="frontend")


@api.get("/api/geocode")
async def api_geocode(address: str):
    return await geocode(address)

@api.get("/api/poi")
async def api_poi(keywords: str, city: str):
    return await poi_search(keywords, city)

@api.get("/api/route")
async def api_route(origin: str, destination: str, mode: str = Query(default="driving")):
    """mode: driving/transit/bicycling/walking"""
    handlers = {
        "driving": route_driving,
        "transit": route_transit,
        "bicycling": route_bicycling,
        "walking": route_walking,
    }
    handler = handlers.get(mode, route_driving)
    return await handler(origin, destination)


# 首页重定向到前端
api.get("/")(lambda: HTMLResponse('<script>window.location="/frontend/"</script>'))


def decode_polyline(encoded: str) -> list:
    """高德 polyline 编码解码"""
    if not encoded or len(encoded) < 4:
        return []
    points, index, length = [], 0, len(encoded)
    lat = lng = 0
    while index < length:
        try:
            shift = result = 0
            while True:
                if index >= length: return points
                b = ord(encoded[index]) - 63; index += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20: break
            lng += ~(result >> 1) if (result & 1) else (result >> 1)
            shift = result = 0
            while True:
                if index >= length: return points
                b = ord(encoded[index]) - 63; index += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20: break
            lat += ~(result >> 1) if (result & 1) else (result >> 1)
            points.append([lng / 1e5, lat / 1e5])
        except Exception:
            continue
    return points


if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=8000)

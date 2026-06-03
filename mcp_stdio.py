"""MCP stdio 入口 — 给 WorkBuddy/Claude 等 AI 客户端用
支持：地理编码、逆编码、驾车/公交/骑行/步行路径规划、POI搜索、周边搜索"""
import httpx
import os
from fastmcp import FastMCP
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
    """将经纬度坐标转换为详细地址。输入经纬度，返回省市区街道信息。"""
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
                "township": ac.get("township", ""),
                "street": info.get("streetNumber", {}).get("street", {}).get("name", ""),
                "nearby_pois": aois,
            }
        return {"error": "逆地理编码失败"}


async def _resolve_location(address: str) -> tuple:
    """内部工具：地址→坐标字符串，失败返回None"""
    geo = await geocode(address)
    if "error" in geo:
        return None, geo
    return f"{geo['longitude']},{geo['latitude']}", geo


# ========== 路径规划 ==========

@mcp.tool()
async def route_driving(origin: str, destination: str) -> dict:
    """规划驾车路线。输入出发地和目的地中文地址。返回距离(公里)、耗时(分钟)、过路费、路线折线坐标(可画地图)、红绿灯数量、逐段导航指引。"""
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
            all_points = []
            steps_desc = []
            for step in route.get("steps", []):
                steps_desc.append({
                    "instruction": step.get("instruction", ""),
                    "road": step.get("road", ""),
                    "distance_m": step.get("distance", 0),
                    "duration_s": step.get("duration", 0)
                })
                if step.get("polyline"):
                    points = decode_polyline(step["polyline"])
                    all_points.extend(points)

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
    """规划公交/地铁换乘路线。输入出发地、目的地中文地址，可选城市名(默认自动识别)。返回多个换乘方案含票价、步行距离、每段详情。"""
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
            return {"error": "未找到公交路线，两点之间可能无直达或换乘公交", "status": "empty"}

        transits = data["route"]["transits"]
        results = []

        for idx, tr in enumerate(transits[:3]):  # 取前3个方案
            segments = []
            total_walk_dist = 0
            for seg in tr.get("segments", []):
                if seg.get("walking"):
                    walk = seg["walking"]
                    wdist = int(walk.get("distance", 0))
                    segments.append({
                        "type": "步行",
                        "distance_m": wdist,
                        "duration_s": int(walk.get("duration", 0)),
                        "guide": walk.get("guide", "")
                    })
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
                        "via_count": len(line.get("via_stops", []))
                    })

            dur_s = int(tr.get("duration", 0))
            dur_h = dur_s // 3600
            dur_m = (dur_s % 3600) // 60
            walk_km = round(total_walk_dist / 1000, 1)
            cost = float(tr.get("cost", 0))

            if dur_h > 0:
                time_str = f"约{dur_h}小时{dur_m}分钟"
            else:
                time_str = f"约{dur_m}分钟"

            results.append({
                "scheme_id": idx + 1,
                "total_duration": time_str,
                "total_seconds": dur_s,
                "walking_distance_km": walk_km,
                "cost_yuan": cost,
                "segments": segments,
                "summary": f"方案{idx+1}: {time_str}, 步行{walk_km}km, 票价{cost}元"
            })

        best = results[0]
        return {
            "mode": "公交/地铁",
            "origin": o_geo.get("address", origin),
            "destination": d_geo.get("address", destination),
            "best_scheme": best,
            "alternative_schemes": results[1:],
            "total_alternatives": len(results),
            "city": use_city,
            "status": "success"
        }


@mcp.tool()
async def route_bicycling(origin: str, destination: str) -> dict:
    """规划骑行路线。注意：若高德骑行服务不可用，将根据直线距离自动估算骑行时间和路线。"""
    o_loc, o_geo = await _resolve_location(origin)
    d_loc, d_geo = await _resolve_location(destination)
    if not o_loc or not d_loc:
        return {"error": "地址解析失败", "status": "failed"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/direction/bicycling", params={
            "key": AMAP_KEY, "origin": o_loc,
            "destination": d_loc, "output": "JSON"
        })
        data = resp.json()

        # 高德骑行服务可能不可用，做降级处理
        if data["status"] != "1":
            # 降级：用驾车距离 + 直线坐标估算
            # 先尝试获取驾车距离作为参考
            try:
                r2 = await client.get(f"{BASE_URL}/direction/driving", params={
                    "key": AMAP_KEY, "origin": o_loc,
                    "destination": d_loc, "output": "JSON"
                })
                d2 = r2.json()
                if d2["status"] == "1":
                    drive_dist = int(d2["route"]["paths"][0]["distance"])
                else:
                    drive_dist = None
            except Exception:
                drive_dist = None

            if drive_dist:
                dist_km = round(drive_dist / 1000, 1)
            else:
                # 最后兜底：用直线距离 * 1.3 估算法
                import math
                o_lng, o_lat = float(o_loc.split(",")[0]), float(o_loc.split(",")[1])
                d_lng, d_lat = float(d_loc.split(",")[0]), float(d_loc.split(",")[1])
                straight = math.sqrt((d_lng - o_lng) ** 2 + (d_lat - o_lat) ** 2) * 111000  # 粗略米
                dist_km = round(straight * 1.3 / 1000, 1)

            # 骑行速度按15km/h算
            speed_kmh = 15
            dur_min = round(dist_km / speed_kmh * 60, 0)

            return {
                "mode": "骑行（估算）",
                "origin": o_geo.get("address", origin),
                "destination": d_geo.get("address", destination),
                "origin_coords": [o_geo["longitude"], o_geo["latitude"]],
                "dest_coords": [d_geo["longitude"], d_geo["latitude"]],
                "distance_km": dist_km,
                "duration_min": dur_min,
                "note": "高德骑行服务暂不可用，已根据实际道路距离估算（骑行平均速度15km/h）",
                "route_polyline": [],  # 无精确路线数据
                "summary": f"骑行约{dist_km}公里，估算{int(dur_min)}分钟（基于道路距离估算）",
                "status": "estimated"
            }

        # 正常返回
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
            "distance_km": dist_km,
            "duration_min": dur_min,
            "route_polyline": all_points[:500],
            "summary": f"骑行约{dist_km}公里，约{dur_min}分钟",
            "status": "success"
        }


@mcp.tool()
async def route_walking(origin: str, destination: str) -> dict:
    """规划步行路线。输入出发地和目的地中文地址。返回距离(公里)、时间(分钟)和路线折线坐标(可画地图)。"""
    o_loc, o_geo = await _resolve_location(origin)
    d_loc, d_geo = await _resolve_location(destination)
    if not o_loc or not d_loc:
        return {"error": "地址解析失败", "status": "failed"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/direction/walking", params={
            "key": AMAP_KEY, "origin": o_loc,
            "destination": d_loc, "output": "JSON"
        })
        data = resp.json()
        if data["status"] == "1":
            route = data["route"]["paths"][0]
            all_points = []
            for step in route.get("steps", []):
                if step.get("polyline"):
                    all_points.extend(decode_polyline(step["polyline"]))
            dist_km = round(int(route["distance"]) / 1000, 1)
            dur_min = round(int(route["duration"]) / 60, 1)

            # 步行速度校验：正常步行5km/h，如果结果异常则标注
            if dist_km > 0 and dur_min / dist_km < 6:  # 少于6分钟/km，不太正常
                note = ""
            else:
                note = ""

            return {
                "mode": "步行",
                "origin": o_geo.get("address", origin),
                "destination": d_geo.get("address", destination),
                "origin_coords": [o_geo["longitude"], o_geo["latitude"]],
                "dest_coords": [d_geo["longitude"], d_geo["latitude"]],
                "distance_km": dist_km,
                "duration_min": dur_min,
                "route_polyline": all_points[:500],
                "summary": f"步行约{dist_km}公里，约{int(dur_min)}分钟",
                "status": "success"
            }
        return {"error": f"步行路径规划失败: {data.get('info', '未知错误')}", "status": "failed"}


# ========== POI 搜索 ==========

@mcp.tool()
async def poi_search(keywords: str, city: str, poi_type: str = "") -> dict:
    """搜索指定城市的兴趣点（餐厅/酒店/景点/加油站等）。可指定类型如：餐饮服务|酒店|风景名胜。"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/place/text", params={
            "key": AMAP_KEY, "keywords": keywords, "city": city,
            "types": poi_type, "output": "JSON", "offset": 10, "page": 1,
            "extensions": "all"
        })
        data = resp.json()
        if data["status"] == "1":
            results = [{"name": p["name"], "address": p["address"],
                        "location": p["location"],
                        "tel": p.get("tel", "无"),
                        "type": p.get("type", ""),
                        "rating": p.get("biz_ext", {}).get("rating", "无")}
                       for p in data["pois"][:5]]
            return {"count": data["count"], "pois": results, "keywords": keywords, "city": city}
        return {"error": "POI搜索失败"}


@mcp.tool()
async def nearby_search(longitude: float, latitude: float, keywords: str,
                        radius: int = 1000) -> dict:
    """搜索某坐标周边的设施（默认半径1公里）。"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/place/around", params={
            "key": AMAP_KEY,
            "location": f"{longitude},{latitude}",
            "keywords": keywords, "radius": radius,
            "output": "JSON", "offset": 8, "extensions": "base"
        })
        data = resp.json()
        if data["status"] == "1":
            results = [{"name": p["name"], "distance_m": p.get("distance", "?"),
                        "address": p["address"], "location": p.get("location", "")}
                       for p in data["pois"][:8]]
            return {"count": data["count"], "center": [longitude, latitude],
                    "nearby": results, "radius_m": radius}
        return {"error": "周边搜索失败"}


# ========== 辅助函数 ==========

def decode_polyline(encoded: str) -> list:
    """高德 polyline 编码解码为 [[lng,lat], ...] 坐标列表"""
    if not encoded or len(encoded) < 4:
        return []
    points = []
    index = 0
    length = len(encoded)
    lat = 0; lng = 0

    while index < length:
        try:
            shift = 0; result = 0
            while True:
                if index >= length:
                    return points
                b = ord(encoded[index]) - 63; index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20: break
            lng += ~(result >> 1) if (result & 1) else (result >> 1)

            shift = 0; result = 0
            while True:
                if index >= length:
                    return points
                b = ord(encoded[index]) - 63; index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20: break
            lat += ~(result >> 1) if (result & 1) else (result >> 1)

            points.append([lng / 1e5, lat / 1e5])
        except Exception:
            continue

    return points


if __name__ == "__main__":
    mcp.run()

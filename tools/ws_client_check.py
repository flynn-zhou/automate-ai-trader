import asyncio, aiohttp, json, time

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("ws://127.0.0.1:8091/ws") as ws:
            t0 = time.time(); counts = {}
            while time.time() - t0 < 12:
                try:
                    msg = await ws.receive(timeout=3)
                except asyncio.TimeoutError:
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    print("closed", msg.type); break
                d = json.loads(msg.data)
                t = d.get("type")
                counts[t] = counts.get(t, 0) + 1
                if t == "hello":
                    s0 = d["snapshot"]; print("hello: src=%s price=%s" % (s0.get("src"), s0.get("price")))
                elif t == "history":
                    print("history %sms: %d bars" % (d["interval_ms"], len(d["bars"])))
                elif t == "bar" and d["i"] == 3000 and d["x"]:
                    print("  3s closed bar %s c=%s n=%s buy=%.3f sell=%.3f" % (
                        time.strftime("%H:%M:%S", time.localtime(d["t"]/1000)), d["c"], d["n"], d["b"], d["s"]))
                elif t == "state":
                    b3 = (d.get("bars") or {}).get("3000") or {}
                    print("  state src=%s px=%s ticks=%s 3s(n=%s)" % (d.get("src"), d.get("price"), d.get("ticks_total"), b3.get("n")))
            print("counts:", counts)

asyncio.run(main())

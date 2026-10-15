from aiohttp import web

def create_app(manager):
    async def command(request):
        payload = await request.json()
        text = payload.get("command", "")
        if text not in {"/status", "/positions", "/leader_positions", "/leader_positons", "/pause", "/resume"}:
            raise web.HTTPBadRequest(text="Unknown command")
        return web.Response(text=await manager.handle(text))

    app = web.Application()
    app.router.add_post("/command", command)
    return app

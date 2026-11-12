from pathlib import Path
from aiohttp import web

def create_app(manager, console=None):
    async def command(request):
        payload = await request.json()
        text = payload.get("command", "")
        if text not in {"/status", "/positions", "/leader_positions", "/leader_positons", "/pause", "/resume"}:
            raise web.HTTPBadRequest(text="Unknown command")
        return web.Response(text=await manager.handle(text))

    app = web.Application()
    app.router.add_post("/command", command)
    if console:
        console.install(app)
    static = Path(__file__).with_name("static")
    app.router.add_get("/", lambda request: web.FileResponse(static / "index.html"))
    app.router.add_static("/static/", static)
    return app

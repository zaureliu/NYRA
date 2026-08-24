from fastapi import Request


def services(request: Request):
    return request.app.state.services


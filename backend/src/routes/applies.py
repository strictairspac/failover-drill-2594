from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from src.config.database import get_db
from src.middlewares.auth import current_user, writable, admin
from src.services.apply_service import (request_apply, list_applies, get_apply,
                                        recover_stale, apply_record, ApplyConflict)
from src.services.drill_service import get_drill
from src.controllers.actions import audit

router = APIRouter()


@router.get('/applies')
def listing(db=Depends(get_db), user=Depends(current_user)):
    return list_applies(db)


@router.get('/drills/{drill_id}/apply')
def detail(drill_id: int, db=Depends(get_db), user=Depends(current_user)):
    get_drill(db, drill_id)
    obj = get_apply(db, drill_id)
    if obj is None: raise HTTPException(404, '该演练尚未发起落实')
    return apply_record(obj)


@router.post('/drills/{drill_id}/apply')
def apply(drill_id: int, db=Depends(get_db), user=Depends(writable)):
    try:
        payload, status = request_apply(db, drill_id, user.email)
    except ApplyConflict as exc:
        db.rollback()
        return JSONResponse({'drill_id': drill_id, 'status': 'conflict',
                             'problems': [str(exc)], 'changes': [], 'simulation': True},
                            status_code=409)
    if payload is None:
        db.rollback()
        raise HTTPException(404, '演练不存在')
    if status == 409 and payload.get('status') != 'rejected':
        db.rollback()
        raise HTTPException(409, '落实凭据冲突，请刷新后重试')
    # request_apply 已在内部提交；拒绝结果用 409 表达“保留原结论但不落实”。
    return JSONResponse(payload, status_code=status)


@router.post('/applies/recover')
def recover(db=Depends(get_db), user=Depends(admin)):
    try:
        recovered = recover_stale(db)
        audit(db, user, f'applies:recover:{len(recovered)}')
        db.commit()
    except ApplyConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc))
    return {'recovered': recovered}

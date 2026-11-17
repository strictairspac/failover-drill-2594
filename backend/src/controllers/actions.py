from src.models.entities import Audit

def audit(db, user, action):
    actor = user if isinstance(user, str) else user.email
    db.add(Audit(actor=actor, action=action))

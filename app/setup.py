"""Company setup: business roles, request-type rules and policies are data an admin edits. Validation lives here so every edit is checked the same way."""
import time

DEFAULT_ROLES = [("it_admin", "IT admin"), ("office_admin", "Office admin"), ("hr_lead", "HR lead"), ("finance_lead", "Finance lead")]


def seed_roles(store):
    """The four built-in business roles exist in every company from the start, with no holder."""
    for code, name in DEFAULT_ROLES:
        store.q("INSERT OR IGNORE INTO roles(code,name,holder_id,builtin,created_at) VALUES(?,?,NULL,1,?)", (code, name, time.time()))

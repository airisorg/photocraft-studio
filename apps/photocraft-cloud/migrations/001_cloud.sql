-- A separate application schema. No changes to auth, storage or another app's data.
CREATE SCHEMA IF NOT EXISTS photocraft;
REVOKE ALL ON SCHEMA photocraft FROM PUBLIC;
CREATE TABLE IF NOT EXISTS photocraft.accounts (
 id uuid PRIMARY KEY, email text NOT NULL, name text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS photocraft.sessions (
 hash text PRIMARY KEY, account_id uuid NOT NULL REFERENCES photocraft.accounts(id),
 expires_at timestamptz NOT NULL DEFAULT now() + interval '7 days'
);
CREATE TABLE IF NOT EXISTS photocraft.projects (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES photocraft.accounts(id),
 title text NOT NULL CHECK(length(title) BETWEEN 1 AND 160),
 folder text NOT NULL DEFAULT '' CHECK(length(folder)<=100),
 starred boolean NOT NULL DEFAULT false, trashed boolean NOT NULL DEFAULT false,
 revision bigint NOT NULL DEFAULT 0, width integer NOT NULL DEFAULT 0,
 height integer NOT NULL DEFAULT 0, thumbnail bytea,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS photocraft.members (
 project_id uuid NOT NULL REFERENCES photocraft.projects(id) ON DELETE CASCADE,
 email text NOT NULL, role text NOT NULL CHECK(role IN ('view','edit')),
 PRIMARY KEY(project_id,email)
);
CREATE TABLE IF NOT EXISTS photocraft.versions (
 project_id uuid NOT NULL REFERENCES photocraft.projects(id) ON DELETE CASCADE,
 revision bigint NOT NULL, author_id uuid NOT NULL REFERENCES photocraft.accounts(id),
 title text NOT NULL, data bytea NOT NULL, sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(project_id,revision)
);
CREATE TABLE IF NOT EXISTS photocraft.uploads (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES photocraft.projects(id) ON DELETE CASCADE,
 author_id uuid NOT NULL REFERENCES photocraft.accounts(id), base_revision bigint NOT NULL,
 bytes bigint NOT NULL CHECK(bytes BETWEEN 1 AND 104857600),
 parts integer NOT NULL CHECK(parts BETWEEN 1 AND 200), sha256 text NOT NULL,
 title text NOT NULL, width integer NOT NULL, height integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS photocraft.chunks (
 upload_id uuid NOT NULL REFERENCES photocraft.uploads(id) ON DELETE CASCADE,
 part integer NOT NULL CHECK(part BETWEEN 0 AND 199), data bytea NOT NULL CHECK(octet_length(data)<=524288),
 PRIMARY KEY(upload_id,part)
);
CREATE TABLE IF NOT EXISTS photocraft.shares (
 hash text PRIMARY KEY, project_id uuid NOT NULL REFERENCES photocraft.projects(id) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS photocraft.comments (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES photocraft.projects(id) ON DELETE CASCADE,
 author_id uuid NOT NULL REFERENCES photocraft.accounts(id),
 body text NOT NULL CHECK(length(body) BETWEEN 1 AND 4000), resolved boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS photocraft.presence (
 project_id uuid NOT NULL REFERENCES photocraft.projects(id) ON DELETE CASCADE,
 account_id uuid NOT NULL REFERENCES photocraft.accounts(id),
 seen_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(project_id,account_id)
);
CREATE INDEX IF NOT EXISTS projects_owner ON photocraft.projects(owner_id);
CREATE INDEX IF NOT EXISTS members_email ON photocraft.members(email);
CREATE INDEX IF NOT EXISTS comments_project ON photocraft.comments(project_id,created_at);
-- Browser clients never access these tables directly. The authenticated HTTP service checks
-- membership for every operation. RLS is defense in depth against an accidental REST grant.
ALTER TABLE photocraft.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE photocraft.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE photocraft.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE photocraft.members ENABLE ROW LEVEL SECURITY;
ALTER TABLE photocraft.versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE photocraft.uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE photocraft.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE photocraft.shares ENABLE ROW LEVEL SECURITY;
ALTER TABLE photocraft.comments ENABLE ROW LEVEL SECURITY;
ALTER TABLE photocraft.presence ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA photocraft FROM PUBLIC;

CREATE TABLE IF NOT EXISTS photocraft.invitation_deliveries (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES photocraft.projects(id) ON DELETE CASCADE,
 sender_id uuid NOT NULL REFERENCES photocraft.accounts(id), email text NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','failed')),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS invitation_sender ON photocraft.invitation_deliveries(sender_id,created_at);
CREATE INDEX IF NOT EXISTS invitation_recipient ON photocraft.invitation_deliveries(email,created_at);
ALTER TABLE photocraft.invitation_deliveries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON photocraft.invitation_deliveries FROM PUBLIC;

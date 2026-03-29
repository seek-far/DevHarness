import os
import subprocess
import datetime
from pathlib import Path
import re
import requests
import base64
import shutil
import logging
import sys
import stat
import platform

#from bf_agent_settings import bf_agent_config
from pathlib import Path
sys.path.append(str(Path.cwd().parent))
from settings import worker_cfg as cfg
  
logger = logging.getLogger(__name__)

def on_rm_error(func, path, exc_info):
    # Remove read-only attribute
    os.chmod(path, stat.S_IWRITE)
    func(path)
    
def _to_ssh_url(repo_url):
    """
    Convert http://host/path  →  ssh://git@host:2222/path.git
      path = group/repo
    """
    m = re.match(r"https?://([^/]+)/(.+)$", repo_url)
    if not m:
        raise ValueError(f"Invalid repo_url format: {repo_url}")

    host, path = m.groups()

    ssh_port = cfg.gitlab_ssh_port #os.environ.get("GITLAB_SSH_PORT", "2222")

    # Non-port-22 connections must use the ssh:// syntax
    return f"ssh://git@{host}:{ssh_port}/{path}.git"
    
class Repo:
    def __init__(self, repo_path: str, repo_url: str):
        self.repo_path = Path(repo_path)
        if cfg.env == "local_multi_process":
            self.repo_url = repo_url.replace("gitlab.local","localhost:8080")
        elif cfg.env == 'local_docker_compose':
            self.repo_url = repo_url.replace("gitlab.local","gitlab")
            self.ssh_url = _to_ssh_url(self.repo_url)
        elif cfg.env == 'local_ts_host':
            if not "8080" in repo_url:
                self.repo_url = repo_url.replace("xxx.tailnnn.ts.net" , "xxx.tailnnn.ts.net:8080")
            self.ssh_url = _to_ssh_url(repo_url) #input to _to_ssh_url should not contain 8080
            logger.debug(f"self.ssh_url={self.ssh_url}")
        elif cfg.env == 'local_ts_host__aca':
            self.repo_url = repo_url.replace(cfg.gitlab_fqdn, cfg.gitlab_ip)
            self.ssh_url = _to_ssh_url(self.repo_url) #input to _to_ssh_url should not contain 8080
            if not "8080" in repo_url:
                self.repo_url = self.repo_url.replace(cfg.gitlab_ip , f"{cfg.gitlab_ip}:8080")
            logger.debug(f"self.repo_url={self.repo_url},self.ssh_url={self.ssh_url}")
            
        self.token = cfg.gitlab_private_token#os.environ["GITLAB_PRIVATE_TOKEN"]
    
    def run(self, *args, cwd=None):
        """Run a git command in target directory."""
        env = os.environ.copy()
        # Disable host key checking globally (simplest approach during the debug phase)
        env.setdefault(
            "GIT_SSH_COMMAND",
            "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        )
        
        args_list = list(args)
        if cfg.env == "local_ts_host__aca" :
            socks5_proxy = cfg.socks5_proxy
            if args_list[0]=="-c" and args_list[2]=="clone": 
                args_list[1] += f' -o "ProxyCommand=nc -X 5 -x {socks5_proxy} %h %p"'
            elif args_list[0] in ["fetch", "push", "pull"]:
                #args_list[:0] = ["-c", f'core.sshCommand=ssh -T -p 2222 "ProxyCommand=nc -X 5 -x {socks5_proxy} %h %p"']
                args_list[:0] = ["-c", f'core.sshCommand=ssh -T -p 2222  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ProxyCommand="nc -X 5 -x {socks5_proxy} %h %p"']

            logger.debug("args_list=======")    
            for (i, arg) in enumerate(args_list):
                logger.debug(f"  {i}:{arg}")
        result = subprocess.run(
            #["git", *args],
            ["git"] + args_list,
            cwd=cwd or self.repo_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.debug(f"Git error result={result}")
            raise RuntimeError(f"Git error: {result.stderr.strip()}")
        if True:
            logger.debug(f"args={args}\nstdout={result.stdout.strip()}")
        return result.stdout.strip()

    def ensure_origin_ssh(self):
        self.run("remote", "set-url", "origin", self.ssh_url)
        
    # ---------------------------
    # 🧩 1️⃣ Check/prepare repository
    # ---------------------------
    def ensure_repo_ready(self):
        if os.path.exists(self.repo_path):
            shutil.rmtree(self.repo_path, onerror=on_rm_error)        
        """Ensure the directory is a valid Git repo; clone if needed."""
        if not (self.repo_path / ".git").exists():
            logger.debug(f"⚙️ No .git found in {self.repo_path}, cloning...")
            self.repo_path.mkdir(parents=True, exist_ok=True)
            # clone into the directory itself
            # if os.environ.get("ENV", "") != "local_ts_host":
            if cfg.env not in ["local_ts_host", "local_ts_host__aca", "local_docker_compose"]: #os.environ.get("ENV", "")
                auth_url = self.repo_url.replace("http://", f"http://{cfg.gitlab_username}:{self.token}@")
                self.run("clone", auth_url, ".", cwd=self.repo_path)
            elif cfg.env == "local_docker_compose":
                auth_url = self.repo_url.replace("http://", f"http://{cfg.gitlab_username}:{self.token}@")
                self.run("clone", auth_url, ".", cwd=self.repo_path)
                self.ensure_origin_ssh()
            else:
                self.run("-c", "core.sshCommand=ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null", "clone", self.ssh_url, ".", cwd=self.repo_path)
                self.ensure_origin_ssh()
            #self.run("clone", self.repo_url, ".", cwd=self.repo_path)        
        else:
            if cfg.env in ["local_ts_host", "local_ts_host__aca"]: #os.environ.get("ENV", "")
                self.ensure_origin_ssh()            
            logger.debug(f"✅ Found existing Git repo at {self.repo_path}")

    # ---------------------------
    # 🧩 2️⃣ Ensure main/master branch exists
    # ---------------------------
    # def ensure_base_branch(self, base_branch="main"):
    #     """Ensure base branch exists locally, fallback to master if needed."""
    #     try:
    #         self.run("fetch", "origin")
    #         self.run("checkout", base_branch)
    #     except RuntimeError:
    #         logger.debug(f"⚠️ '{base_branch}' not found locally, checking remote...")
    #         # Detect remote branches
    #         remote_branches = self.run("branch", "-r")
    #         if f"origin/{base_branch}" in remote_branches:
    #             self.run("checkout", "-b", base_branch, f"origin/{base_branch}")
    #         elif "origin/master" in remote_branches:
    #             logger.debug("🔁 Falling back to 'master'")
    #             self.run("checkout", "-b", "main", "origin/master")
    #         else:
    #             raise RuntimeError("❌ Neither main nor master branch found in remote.")
    # 
    #     # Pull latest
    #     self.run("pull", "origin", base_branch)
    #     logger.debug(f"✅ Base branch ready: {base_branch}")
    
    # https://chatgpt.com/g/g-p-68d6aea6596081918d694b543728ecef-job-hunting/c/6918f511-3368-8325-9eea-12fdc32b9a97
    def ensure_base_branch(self, base_branch="main"):
        """
        Ensure the base branch exists locally and is up-to-date.
        Handles:
          - local branch exists
          - only remote branch exists
          - fallback to master
          - fetch failure should NOT kill workflow
        """
        # ---------- STEP 1: Try safe fetch ----------
        try:
            self.run("fetch", "origin")
        except RuntimeError as e:
            logger.debug(f"⚠️ git fetch failed, but continuing: {e}")
    
        # ---------- STEP 2: Detect remote branches ----------
        remote_branches = self.run("branch", "-r")
    
        has_main_remote = f"origin/{base_branch}" in remote_branches
        has_master_remote = "origin/master" in remote_branches
    
        # ---------- STEP 3: Detect local branches ----------
        local_branches = self.run("branch")
    
        has_main_local = base_branch in local_branches
    
        # ---------- STEP 4: Checkout logic ----------
        if has_main_local:
            # Local main already exists — switch directly (don't create)
            self.run("checkout", base_branch)
    
        else:
            # No local main — create it from the remote
            if has_main_remote:
                self.run("checkout", "-b", base_branch, f"origin/{base_branch}")
            elif has_master_remote:
                logger.debug("🔁 Falling back to 'master'")
                self.run("checkout", "-b", base_branch, "origin/master")
            else:
                raise RuntimeError("❌ Neither main nor master branch found in remote.")
    
        # ---------- STEP 5: Try pull but tolerate failure ----------
        try:
            self.run("pull", "origin", base_branch)
        except RuntimeError as e:
            logger.debug(f"⚠️ git pull failed (ignored): {e}")
    
        logger.debug(f"✅ Base branch ready: {base_branch}")
    

    # ---------------------------
    # 🧩 3️⃣ Create fix branch
    # ---------------------------
    def create_fix_branch(self, bug_id=None, base_branch="main"):
        """Create and push a fix branch."""
        self.ensure_repo_ready()
        self.ensure_base_branch(base_branch)

        #ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        #branch_name = f"fix/{issue_id}_{ts}" if issue_id else f"fix/auto_{ts}"
        now = datetime.datetime.now()
        branch_id = now.strftime("%H_%M_%S") + f"_{now.microsecond // 100000}"
        branch_name = f"auto/bug_{bug_id}-patch_{branch_id}"

        self.run("checkout", "-b", branch_name)
        # self.run("push", "-u", "origin", branch_name)
        current_commit = self.run("rev-parse", "HEAD")

        logger.debug(f"✅ Created branch {branch_name} from {base_branch}")
        return {
            "status": "success",
            "branch_name": branch_name,
            "base_branch": base_branch,
            "commit": current_commit,
        }

    def commit_changes(self, message: str = "ci_agent: auto commit changes"):
        """Commit and push local changes."""
        # 1️⃣ Ensure we are inside a git repository
        if not (self.repo_path / ".git").exists():
            raise RuntimeError(f"Not a git repo: {self.repo_path}")

        # 2️⃣ Check if there are any changes
        status = self.run("status", "--porcelain")
        if not status:
            logger.debug("ℹ️ No changes to commit.")
            return {"status": "no_changes"}

        logger.debug("🪶 Changes detected, committing...")

        # 3️⃣ Stage changes
        self.run("add", "-A")

        # 4️⃣ Create commit
        try:
            self.run("commit", "-m", message)
        except RuntimeError as e:
            if "nothing to commit" in str(e):
                logger.debug("ℹ️ Nothing to commit.")
                return {"status": "no_changes"}
            raise

        # 5️⃣ Get current branch name
        branch = self.run("rev-parse", "--abbrev-ref", "HEAD")

        # 6️⃣ Push
        logger.debug(f"🚀 Pushing to origin/{branch} ...")
        self.run("push", "origin", branch)

        # 7️⃣ Return info
        commit_hash = self.run("rev-parse", "HEAD")
        logger.debug(f"✅ Pushed commit {commit_hash[:8]} to {branch}")
        return {
            "status": "success",
            "branch": branch,
            "commit": commit_hash,
        }

    def gitlab_create_merge_request(self, source_branch: str, target_branch: str = "main", title: str = None, description: str = None):
        """
        Create a merge request in GitLab using REST API.
    
        Args:
            repo_url: e.g. "http://localhost:8080/root/order_be.git"
            source_branch: feature or fix branch name
            target_branch: usually 'main'
            title: merge request title
            description: optional description text
        """
        if not self.token:
            raise EnvironmentError("❌ Missing GITLAB_TOKEN environment variable")
    
        # 1️⃣ Extract GitLab host and project path from repo_url
        # e.g. http://localhost:8080/root/order_be.git → host=http://localhost:8080, path=root/order_be
        #m = re.match(r"(https?://[^/]+)/(.+)\.git", self.repo_url)
        m = re.match(r"(https?://[^/]+)/(.+?)(?:\.git)?$", self.repo_url)
        if not m:
            raise ValueError(f"Invalid repo_url format: {self.repo_url}")
    
        host, project_path = m.groups()
    
        # GitLab API requires project_id = URL-encoded path (e.g. root%2Forder_be)
        project_id = project_path.replace("/", "%2F")
    
        api_url = f"{host}/api/v4/projects/{project_id}/merge_requests"
    
        headers = {"PRIVATE-TOKEN": self.token}
        data = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title or f"Merge {source_branch} into {target_branch}",
            "description": description or "Auto-created by ci_agent",
            "remove_source_branch": True,  # optional: delete source branch after merge
        }
    
        logger.debug(f"📤 Creating Merge Request at {api_url}")
        if cfg.env != "local_ts_host__aca" :
            resp = requests.post(api_url, headers=headers, data=data)
        else:
            proxies = {
                "http":  f"socks5://{cfg.socks5_proxy}",
                "https": f"socks5://{cfg.socks5_proxy}",
            }        
            resp = requests.post(api_url, headers=headers, data=data, proxies=proxies)
        
        if resp.status_code != 201:
            raise RuntimeError(f"GitLab API error {resp.status_code}: {resp.text}")
    
        mr = resp.json()
        logger.debug(f"✅ Merge Request created: !{mr['iid']} → {mr['web_url']}")
        return {
            "id": mr["id"],
            "iid": mr["iid"],
            "title": mr["title"],
            "url": mr["web_url"],
            "state": mr["state"],
        }

    def gitlab_fetch_file(
        self,
        file_path: str,
        branch: str = "main",
    ):
        """
        Fetch the content of a specified file from a GitLab repository.

        Args:
            file_path: path of the file in the repository, e.g. "api/views.py"
            branch: branch name, default main

        Returns:
            file content as a string

        self.token: Personal Access Token or Runner Token
        """
        # extract host and project_path from repo_url
        # host: GitLab instance address, e.g. "http://localhost:8080"
        # project_path: repository ID or namespace path, e.g. "lishu2016/order_be"
        # assuming repo_url does not have ".git" in the end
        m = re.match(r"(https?://[^/]+)/(.+?)(?:\.git)?$", self.repo_url)
        if not m:
            raise ValueError(f"Invalid repo_url format: {self.repo_url}")
    
        host, project_path = m.groups()
        url = f"{host}/api/v4/projects/{requests.utils.quote(project_path, safe='')}/repository/files/{requests.utils.quote(file_path, safe='')}"
        logger.debug(f"[gitlab_fetch_file], self.repo_url={self.repo_url}, host={host}, project_path={project_path},url={url}")
        params = {"ref": branch}
        headers = {"PRIVATE-TOKEN": self.token} if self.token else {}
    
        if cfg.env != "local_ts_host__aca" :
            response = requests.get(url, headers=headers, params=params)
        else:
            proxies = {
                "http":  f"socks5://{cfg.socks5_proxy}",
                "https": f"socks5://{cfg.socks5_proxy}",
            }
            response = requests.get(url, headers=headers, params=params, proxies=proxies)

        if response.status_code != 200:
            raise Exception(f"❌ Failed to fetch file: {response.status_code} {response.text}")
    
        data = response.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        return content
        
if __name__ == "__main__":
    import sys
    #if len(sys.argv) < 3:
    #    logger.debug("Usage: python create_fix_branch.py <repo_path> <repo_url> [issue_id]")
    #    sys.exit(1)
    if len(sys.argv) > 1:
        repo_path = sys.argv[1]  
    else:
        repo_path = '/my_git/restaurant_order_demo__order_be_bf'
        if platform.system() == "Linux":
            repo_path =  "/mnt/d" + repo_path        
    
    repo_url = sys.argv[2] if len(sys.argv) > 2 else 'http://localhost:8080/lishu2016/order_be'
    issue_id = sys.argv[3] if len(sys.argv) > 3 else None

    repo = Repo(repo_path, repo_url)
    
    #result = repo.create_fix_branch(issue_id)
    #logger.debug(result)
    
    #result_commit_changes = repo.commit_changes()
    
    # result_create_merge_request = repo.gitlab_create_merge_request(source_branch = 'fix/auto_20251109_211244')
    
    file_content = repo.gitlab_fetch_file("api/views.py")
    logger.debug(file_content)

# example
# python create_fix_branch.py D:\ci_agent\repos\order_be https://gitlab.example.com/team/order_be.git  fix-menu-api
function showAuthError(toast, message) {
  toast.textContent = message;
  toast.classList.add("error");
}

function initLoginForm() {
  const form = document.getElementById("login-form");
  if (!form || form.dataset.authBound === "true") return;
  form.dataset.authBound = "true";

  const toast = form.querySelector("#toast");
  const submitBtn = form.querySelector("#submit-btn");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submitBtn.disabled = true;
    toast.textContent = "";
    toast.classList.remove("error");

    try {
      await apiRequest("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
          username: form.querySelector("#username").value.trim(),
          password: form.querySelector("#password").value,
        }),
      });
      markTabAuthenticated();
      await navigateSitePage("/standings.html");
    } catch (err) {
      showAuthError(toast, err.message);
    } finally {
      submitBtn.disabled = false;
    }
  });
}

function initRegisterForm() {
  const form = document.getElementById("register-form");
  if (!form || form.dataset.authBound === "true") return;
  form.dataset.authBound = "true";

  const toast = form.querySelector("#toast");
  const submitBtn = form.querySelector("#submit-btn");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = form.querySelector("#username").value.trim();
    const password = form.querySelector("#password").value;
    const confirmPassword = form.querySelector("#confirm").value;

    toast.textContent = "";
    toast.classList.remove("error");
    if (password !== confirmPassword) {
      showAuthError(toast, "两次输入的密码不一致");
      return;
    }

    submitBtn.disabled = true;
    try {
      await apiRequest("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      toast.textContent = "注册成功，即将切换到登录页…";
      setTimeout(() => navigateSitePage("/login.html"), 600);
    } catch (err) {
      showAuthError(toast, err.message);
    } finally {
      submitBtn.disabled = false;
    }
  });
}

function initGuestPasswordForm() {
  const form = document.getElementById("guest-password-form");
  const modal = document.getElementById("guest-password-modal");
  const openButton = document.getElementById("open-password-btn");
  const closeButton = document.getElementById("close-password-btn");
  if (!form || !modal || !openButton || form.dataset.authBound === "true") return;
  form.dataset.authBound = "true";

  const toast = form.querySelector("#reset-toast");
  const submitButton = form.querySelector("#reset-submit-btn");

  openButton.addEventListener("click", () => {
    toast.textContent = "";
    toast.classList.remove("error");
    modal.showModal();
    form.querySelector("#reset-username").focus();
  });
  closeButton.addEventListener("click", () => modal.close());
  modal.addEventListener("click", (event) => {
    if (event.target === modal) modal.close();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const newPassword = form.querySelector("#reset-new-password").value;
    const confirmPassword = form.querySelector("#reset-confirm-password").value;
    toast.textContent = "";
    toast.classList.remove("error");

    if (newPassword !== confirmPassword) {
      showAuthError(toast, "两次输入的新密码不一致");
      return;
    }

    submitButton.disabled = true;
    try {
      const result = await apiRequest("/api/auth/password", {
        method: "PUT",
        body: JSON.stringify({
          username: form.querySelector("#reset-username").value.trim(),
          current_password: form.querySelector("#reset-current-password").value,
          new_password: newPassword,
        }),
      });
      form.reset();
      toast.textContent = result.message;
      setTimeout(() => modal.close(), 1000);
    } catch (err) {
      showAuthError(toast, err.message);
    } finally {
      submitButton.disabled = false;
    }
  });
}

function initAuthForm() {
  initLoginForm();
  initRegisterForm();
  initGuestPasswordForm();
}

window.initAuthForm = initAuthForm;

(async () => {
  if (document.getElementById("login-form")) {
    const user = await getCurrentUser();
    if (user) {
      await navigateSitePage("/standings.html");
      return;
    }
  }
  initAuthForm();
})();

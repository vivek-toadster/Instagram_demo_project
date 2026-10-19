/* Luma's small interaction layer: actions update in place instead of navigating. */
document.addEventListener("DOMContentLoaded", () => {
  const readResponse = async (response) => {
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      throw new Error(`Request failed (${response.status}). Please refresh and try again.`);
    }
    return response.json();
  };

  // Attach one background-submit handler to every interactive form on the page.
  document.querySelectorAll("form.async-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = form.querySelector("button[type=submit]");
      if (submit) submit.disabled = true;

      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        const data = await readResponse(response);
        if (!response.ok || !data.ok) throw new Error(data.error || "Something went wrong.");

        const card = form.closest("[data-post-id]");
        if (data.likes_count !== undefined && card) {
          const likeButton = card.querySelector(".btn-like");
          likeButton.classList.toggle("liked", data.liked);
          likeButton.querySelector(".like-icon").textContent = data.liked ? "♥" : "♡";
          likeButton.querySelector(".like-count").textContent = data.likes_count;
          likeButton.setAttribute("aria-pressed", data.liked ? "true" : "false");
        }

        if (data.comment && card) {
          let list = card.querySelector(".comments-list");
          if (!list) {
            list = document.createElement("div");
            list.className = "comments-list";
            card.querySelector(".comments-section").prepend(list);
          }
          const item = document.createElement("div");
          item.className = "comment-item comment-new";
          item.innerHTML = `<a href="/profile/${encodeURIComponent(data.comment.username)}" class="comment-username"></a><span class="comment-body"></span><span class="comment-time"> · ${data.comment.created_at}</span>`;
          item.querySelector(".comment-username").textContent = data.comment.username;
          item.querySelector(".comment-body").textContent = data.comment.body;
          list.append(item);
          form.reset();
          list.scrollTop = list.scrollHeight;
        }

        if (data.is_following !== undefined) {
          const button = form.querySelector("button");
          button.textContent = data.is_following ? "Following" : "Follow";
          button.classList.toggle("is-following", data.is_following);
          if (data.followers_count !== undefined) {
            const followersCount = document.querySelector("#profile-followers-count");
            if (followersCount) followersCount.textContent = data.followers_count;
          }
          if (data.following_count !== undefined) {
            const followingCount = document.querySelector("#profile-following-count");
            if (followingCount) followingCount.textContent = data.following_count;
          }
        }

        if (data.comment_id) {
          form.closest(".comment-item")?.remove();
        }
        if (form.classList.contains("delete-form")) {
          card?.classList.add("post-removing");
          setTimeout(() => card?.remove(), 220);
        }
        if (form.classList.contains("message-compose")) {
          const thread = document.querySelector("[data-thread]");
          const list = thread?.querySelector(".message-list");
          const empty = list?.querySelector(".thread-empty");
          if (empty) empty.remove();
          if (list) {
            const row = document.createElement("div");
            row.className = "message-bubble-wrap mine";
            row.setAttribute("data-message-row", "");
            const author = document.createElement("strong");
            author.className = "message-author";
            author.textContent = thread.dataset.currentUsername;
            const bubble = document.createElement("div");
            bubble.className = "message-bubble";
            bubble.textContent = data.message;
            const timestamp = document.createElement("small");
            timestamp.textContent = "now";
            row.append(author, bubble, timestamp);
            list.append(row);
            list.scrollTop = list.scrollHeight;
          }
          form.reset();
        }
      } catch (error) {
        showToast(error.message, true);
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  });

  const assistantForm = document.querySelector("[data-assistant-form]");
  if (assistantForm) {
    assistantForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = assistantForm.querySelector("input[name=question]");
      const answer = document.querySelector("[data-assistant-answer]");
      const history = document.querySelector("[data-assistant-history]");
      const button = assistantForm.querySelector("button");
      button.disabled = true;
      answer.textContent = "Thinking…";
      try {
        const formData = new FormData(assistantForm);
        if (!formData.get("post_id")) formData.delete("post_id");
        const response = await fetch("/chatbot", {
          method: "POST",
          body: formData,
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        const data = await readResponse(response);
        if (!response.ok || !data.ok) throw new Error(data.error || "Assistant unavailable.");
        answer.textContent = data.answer;
        answer.classList.add("assistant-answer-visible");
        if (history) {
          const questionMessage = document.createElement("div");
          questionMessage.className = "assistant-message assistant-message-user";
          questionMessage.textContent = input.value;
          const answerMessage = document.createElement("div");
          answerMessage.className = "assistant-message assistant-message-bot";
          answerMessage.textContent = data.answer;
          history.append(questionMessage, answerMessage);
          history.scrollTop = history.scrollHeight;
        }
        input.value = "";
      } catch (error) {
        answer.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
  }

  document.querySelectorAll("[data-ask-post]").forEach((button) => {
    button.addEventListener("click", () => {
      const postId = document.querySelector("[data-assistant-post-id]");
      const input = document.querySelector("[data-assistant-form] input[name=question]");
      if (postId && input) {
        postId.value = button.dataset.askPost;
        input.value = "What is this post about?";
        input.focus();
      }
    });
  });
});

function showToast(message, isError = false) {
  // Show a short-lived status message when an AJAX action succeeds or fails.
  const toast = document.createElement("div");
  toast.className = `toast${isError ? " toast-error" : ""}`;
  toast.textContent = message;
  document.body.append(toast);
  requestAnimationFrame(() => toast.classList.add("toast-visible"));
  setTimeout(() => toast.remove(), 2800);
}

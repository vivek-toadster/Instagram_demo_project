# Personalized Instagram Feed — Simple Explanation

## 1. Project ka basic idea

Hamari application do databases use karti hai:

- **PostgreSQL:** Main/authoritative database. Users, posts, likes, comments aur follows yahan safely store hote hain.
- **Neo4j:** Graph database. Ye users aur posts ke relationships ko quickly understand karta hai.

PostgreSQL hamesha source of truth hai. Neo4j recommendation ke liye helper database hai.

---

## 2. Feed request hone par kya hota hai?

Jab logged-in user `/feed` open karta hai:

1. Application user ki identity session se read karti hai.
2. Neo4j se relevant post IDs mangi jaati hain.
3. PostgreSQL un post IDs ki actual details fetch karta hai.
4. PostgreSQL likes aur comments ke real counts deta hai.
5. Python ranking formula har post ko score deta hai.
6. Highest-score posts feed mein show hote hain.

Simple flow:

```text
Logged-in user
      |
      v
Neo4j: relevant post candidates
      |
      v
PostgreSQL: real post + likes + comments
      |
      v
Ranking formula
      |
      v
Personalized feed
```

---

## 3. Neo4j mein relationships

Neo4j users, posts aur hashtags ko graph nodes/relationships ke form mein rakhta hai:

```text
(User)-[:FOLLOWS]->(User)
(User)-[:CREATED]->(Post)
(User)-[:LIKED]->(Post)
(User)-[:COMMENTED_ON]->(Post)
(Post)-[:HAS_TAG]->(Hashtag)
```

Example:

```text
Alice -FOLLOWS-> Bob
Bob -CREATED-> CricketPost
Alice -LIKED-> CricketPost
CricketPost -HAS_TAG-> cricket
```

Is graph ki wajah se hum ye questions quickly answer kar sakte hain:

- User kin accounts ko follow karta hai?
- Followed accounts ne kaunse posts banaye?
- Friends-of-friends kaun hain?
- User ne kin topics ya hashtags wale posts like kiye?
- Similar activity wale users kaun hain?

---

## 4. Candidate posts kaise milte hain?

Feed ranking se pehle Neo4j possible posts ki list banata hai. Is list ko **candidate posts** kehte hain.

### A. Direct follows

User jin accounts ko follow karta hai, unke recent posts candidates ban sakte hain.

```text
Current user -> FOLLOWS -> Author -> CREATED -> Post
```

In posts ko strongest social connection milta hai.

### B. Friends-of-friends

Agar user kisi account ko follow karta hai aur woh account kisi aur creator ko follow karta hai, to us creator ke posts bhi candidates ban sakte hain.

```text
Current user -> FOLLOWS -> User A -> FOLLOWS -> User B -> CREATED -> Post
```

In posts ko direct follows se smaller social boost milta hai.

### C. Same liked posts

Agar do users ne similar posts like kiye hain, to doosre user ki liked-content activity se related posts candidates ban sakte hain.

Example:

```text
Alice likes Dog Post 1
Bob also likes Dog Post 1
Bob creates Dog Post 2
Dog Post 2 becomes relevant for Alice
```

### D. Same hashtags

Post captions se hashtags extract hote hain. Examples:

```text
#actor
#fruits
#gym
#cricket
```

Example:

```text
Alice likes a post with #gym
Another post also has #gym
That post gets an interest signal for Alice
```

Hashtag matching ki wajah se user ke interests gradually feed mein reflect hote hain.

---

## 5. Ranking algorithm

Candidate posts ko simple explainable score diya jaata hai. Current weights:

| Signal | Weight | Meaning |
|---|---:|---|
| Recency | 0.45 | Naya post generally upar aata hai |
| Social relationship | 0.25 | Followed account ko zyada importance |
| Engagement | 0.15 | Likes aur comments zyada hone par boost |
| Interest | 0.15 | Same liked posts ya hashtags ka match |

Final score ka simplified formula:

```text
Final Score =
    Recency Score      × 0.45
  + Social Score       × 0.25
  + Engagement Score   × 0.15
  + Interest Score     × 0.15
```

### Recency score

Recent posts ka score high hota hai. Older posts ka score time ke saath gradually reduce hota hai.

### Social score

- Direct followed account: strongest social score
- Friend-of-friend: smaller social score
- Interest-only recommendation: social score low, interest score high ho sakta hai

### Engagement score

Post ke available likes aur comments count hote hain. Counts PostgreSQL se aate hain, Neo4j se nahi.

Bahut high engagement score ko cap kiya jaata hai, taaki ek viral post poori feed dominate na kare.

### Interest score

Interest score tab increase hota hai jab:

- Post ke hashtags user ke liked hashtags se match karein.
- Similar user ne same posts like kiye hon.

Score `0` se `1` ke beech normalize hota hai.

---

## 6. Like karne ke baad feed ka behavior

User ke like karne ke baad:

1. PostgreSQL mein Like record create hota hai.
2. Same transaction mein `like.created` outbox event create hota hai.
3. Outbox worker event ko Neo4j mein apply karta hai.
4. Neo4j mein `(User)-[:LIKED]->(Post)` relationship create hota hai.
5. Future feed requests mein similar hashtags aur similar liked posts ka signal stronger ho sakta hai.

Liked post feed se remove nahi hota. Woh feed mein rehta hai, aur uska like count update hota hai.

---

## 7. Outbox pattern kyun use kiya?

PostgreSQL transaction ke andar Neo4j network call nahi hoti. Isse business data safe rehta hai.

Example:

```text
Like action
   |
   +--> PostgreSQL Like record
   |
   +--> PostgreSQL outbox event
           |
           v
       Background worker
           |
           v
        Neo4j update
```

Agar Neo4j temporarily down ho:

- PostgreSQL like/follow/post action fail nahi hota.
- Event `pending` ya `failed` state mein rehta hai.
- Worker baad mein retry karta hai.
- Maximum retries ke baad event dead-letter state mein ja sakta hai.

Is system ki guarantee **at-least-once delivery** hai, exactly-once nahi. Isliye Neo4j operations `MERGE` jaise idempotent operations use karte hain.

---

## 8. PostgreSQL fallback

Neo4j unavailable hone par application error page nahi dikhati.

Feed PostgreSQL se chronological order mein load hoti hai:

```text
Neo4j available  -> personalized ranked feed
Neo4j unavailable -> recent PostgreSQL feed
```

Isse core application aur existing feed functionality available rehti hai.

---

## 9. Pagination and performance

- Feed `page` aur `limit` support karti hai.
- Page size maximum 100 hai.
- Neo4j candidate query bounded hai.
- Candidate IDs duplicate hone par strongest signal preserve hota hai.
- PostgreSQL post details ek bounded query se load hoti hain.
- Likes aur comments counts aggregate queries se fetch hote hain.
- Deterministic tie-breaker post ID hai, isliye same score par order stable rehta hai.

---

## 10. Cold-start users

New user ke paas likes ya follows nahi honge. Us case mein:

- Followed accounts ke posts use hote hain, agar follows available hain.
- Friends-of-friends candidates use hote hain.
- Graph candidates empty hone par recent PostgreSQL posts fallback ke roop mein show hote hain.

Jaise-jaise user likes aur follows karta hai, interest-based signals available hote jaate hain.

---

## 11. Presentation mein simple explanation

> “Hamari feed pehle graph database se relevant posts discover karti hai. Ye dekhti hai ki user kin accounts ko follow karta hai, uske network mein kaunse posts hain, aur user ne kin posts ya hashtags mein interest dikhaya hai. Uske baad PostgreSQL se real post details, likes aur comments counts fetch hote hain. Finally, recency, relationship, engagement aur interest ko weighted score mein combine karke posts rank kiye jaate hain. Agar Neo4j unavailable ho, system recent PostgreSQL posts show karta hai, isliye application fail nahi hoti.”

---

## 12. Current limitations

- Ye machine-learning model nahi hai; ye explainable weighted ranking hai.
- Hashtag interest caption/image filename se infer hota hai.
- No Redis, Kafka, Celery, or background recommendation model is required.
- PostgreSQL authoritative hai; Neo4j projection delayed ho sakti hai until the outbox worker processes events.
- Exact production-scale recommendation quality ke liye future mein experiments, analytics, and ML models add kiye ja sakte hain.


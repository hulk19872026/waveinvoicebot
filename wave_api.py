"""Thin client for Wave's public GraphQL API (https://gql.waveapps.com/graphql/public)."""
import requests

API_URL = "https://gql.waveapps.com/graphql/public"


class WaveError(Exception):
    pass


class Wave:
    def __init__(self, token, business_id=None, income_account_id=None):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self.business_id = business_id or self._first_business_id()
        self.income_account_id = income_account_id or self._first_income_account_id()
        self.customers, self.products = [], []
        self.refresh()

    # ---------- low level ----------
    def gql(self, query, variables=None):
        r = self.s.post(API_URL, json={"query": query, "variables": variables or {}}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise WaveError("; ".join(e.get("message", "?") for e in data["errors"]))
        return data["data"]

    def _mutate(self, name, input_type, input_obj, returning):
        q = f"""mutation($input: {input_type}!) {{
            {name}(input: $input) {{
                didSucceed
                inputErrors {{ message path }}
                {returning}
            }}
        }}"""
        out = self.gql(q, {"input": input_obj})[name]
        if not out["didSucceed"]:
            errs = "; ".join(f"{'.'.join(e.get('path') or [])}: {e['message']}" for e in out["inputErrors"] or [])
            raise WaveError(f"{name} failed: {errs or 'unknown error'}")
        return out

    def _paged(self, field, fields, extra_args=""):
        out, page = [], 1
        while True:
            q = f"""query($b: ID!, $p: Int!) {{
                business(id: $b) {{
                    {field}(page: $p, pageSize: 100{extra_args}) {{
                        pageInfo {{ totalPages }}
                        edges {{ node {{ {fields} }} }}
                    }}
                }}
            }}"""
            d = self.gql(q, {"b": self.business_id, "p": page})["business"][field]
            out += [e["node"] for e in d["edges"]]
            if page >= (d["pageInfo"]["totalPages"] or 1):
                return out
            page += 1

    # ---------- setup ----------
    def _first_business_id(self):
        d = self.gql("query { businesses(page: 1, pageSize: 20) { edges { node { id name isPersonal } } } }")
        biz = [e["node"] for e in d["businesses"]["edges"] if not e["node"]["isPersonal"]]
        if not biz:
            raise WaveError("No business found on this Wave account.")
        return biz[0]["id"]

    def _first_income_account_id(self):
        accts = self._paged("accounts", "id name isArchived", ", subtypes: [INCOME]")
        accts = [a for a in accts if not a["isArchived"]]
        return accts[0]["id"] if accts else None

    def refresh(self):
        """Reload clients and products from Wave."""
        self.customers = self._paged("customers", "id name email")
        prods = self._paged("products", "id name description unitPrice isSold isArchived defaultSalesTaxes { id }")
        self.products = [p for p in prods if p["isSold"] and not p["isArchived"]]

    # ---------- create ----------
    def create_customer(self, name, email=None, phone=None, first_name=None, last_name=None):
        inp = {"businessId": self.business_id, "name": name}
        for k, v in {"email": email, "phone": phone, "firstName": first_name, "lastName": last_name}.items():
            if v:
                inp[k] = v
        c = self._mutate("customerCreate", "CustomerCreateInput", inp, "customer { id name email }")["customer"]
        self.customers.append(c)
        return c

    def create_product(self, name, unit_price, description=None):
        if not self.income_account_id:
            raise WaveError("No income account found; set WAVE_INCOME_ACCOUNT_ID.")
        inp = {
            "businessId": self.business_id,
            "name": name,
            "unitPrice": str(unit_price),
            "incomeAccountId": self.income_account_id,
        }
        if description:
            inp["description"] = description
        p = self._mutate("productCreate", "ProductCreateInput", inp,
                         "product { id name description unitPrice isSold isArchived defaultSalesTaxes { id } }")["product"]
        self.products.append(p)
        return p

    @staticmethod
    def _items(items):
        out = []
        for it in items:
            row = {
                "productId": it["product_id"],
                "quantity": str(it["quantity"]),
                "unitPrice": str(it["unit_price"]),
            }
            if it.get("description"):
                row["description"] = it["description"]
            if it.get("tax_ids"):
                row["taxes"] = [{"salesTaxId": t} for t in it["tax_ids"]]
            out.append(row)
        return out

    def create_invoice(self, customer_id, items, memo=None):
        inp = {"businessId": self.business_id, "customerId": customer_id,
               "status": "DRAFT", "items": self._items(items)}
        if memo:
            inp["memo"] = memo
        inv = self._mutate("invoiceCreate", "InvoiceCreateInput", inp,
                           "invoice { id invoiceNumber viewUrl pdfUrl total { value currency { code } } }")["invoice"]
        return {"number": inv["invoiceNumber"], "view_url": inv["viewUrl"],
                "pdf_url": inv["pdfUrl"], "total": inv["total"]["value"]}

    def create_estimate(self, customer_id, items, memo=None):
        # Mirrors invoiceCreate. If Wave's schema names differ for your account,
        # adjust the input/returning fields here (see Wave API Reference: EstimateCreateInput).
        inp = {"businessId": self.business_id, "customerId": customer_id, "items": self._items(items)}
        if memo:
            inp["memo"] = memo
        est = self._mutate("estimateCreate", "EstimateCreateInput", inp,
                           "estimate { id estimateNumber viewUrl pdfUrl total { value currency { code } } }")["estimate"]
        return {"number": est["estimateNumber"], "view_url": est["viewUrl"],
                "pdf_url": est["pdfUrl"], "total": est["total"]["value"]}

"""Ten-level call stack with decorated exception handling at depth_5.

The values flowing through the stack are pydantic models, so the inspector has
something realistic to walk: ``cart.customer.tier``, ``cart.subtotal()`` and
``quote.model_dump()`` all work inside the dump, offline, after the process is
gone. The models live in :mod:`example_models` so the dump can store them by
reference; see that module for why that matters.

Running this writes two dumps under two different exception paths -- a
``RuntimeError`` from the checkout flow and a pydantic ``ValidationError`` from
a bad payload -- so ``exception_debugger.py list`` shows how dumps are grouped.
"""

from pydantic import ValidationError

from example_models import Cart, Customer, LineItem, ShippingQuote
from exception_debugger import configure, dump_exception, dump_on_exception

configure(
    max_dumps_per_path=1000,
    # serializer="dill"
    n_depth_down=1,
    n_depth_up=1,
)


def report(trace_id: str) -> None:
    """Stand-in for whatever a service does with a trace id (log, alert, ...)."""
    print(f"[report] captured exception trace {trace_id}")


# -- the failing call stack --------------------------------------------------


def depth_1() -> None:
    request_id = "request-abc-123"
    depth_2(request_id)


def depth_2(request_id: str) -> None:
    customer = Customer(name="alice", request_id=request_id, tier="gold")
    depth_3(customer)


def depth_3(customer: Customer) -> None:
    cart = Cart(
        customer=customer,
        items=[
            LineItem(sku="DESK-1", price=25.0),
            LineItem(sku="CHAIR-2", price=40.0),
            LineItem(sku="LAMP-3", price=30.0, quantity=2),
        ],
    )
    depth_4(cart)


def depth_4(cart: Cart) -> None:
    subtotal = cart.subtotal()
    depth_5(cart, subtotal)



# Three frames down reaches depth_8, where the ShippingQuote is built.
@dump_on_exception(on_dump=report)
def depth_5(cart: Cart, subtotal: float) -> None:
    """The decorator catches and dumps exceptions crossing this boundary."""
    discount = 0.10 if cart.customer.tier == "gold" else 0.0
    depth_6(cart, subtotal, discount)


def depth_6(cart: Cart, subtotal: float, discount: float) -> None:
    discounted_total = subtotal * (1 - discount)
    depth_7(cart, discounted_total)


def depth_7(cart: Cart, discounted_total: float) -> None:
    shipping_zone = "international"
    depth_8(cart, discounted_total, shipping_zone)




def depth_8(cart: Cart, discounted_total: float, shipping_zone: str) -> None:
    zone_divisors = {"domestic": 2, "international": 0}
    quote = ShippingQuote(
        zone=shipping_zone,
        divisor=zone_divisors[shipping_zone],
        discounted_total=discounted_total,
    )
    try:
        depth_9(cart, quote, zone_divisors)
    except ZeroDivisionError as error:
        # Chained on purpose: walk the chain with /exception-up and
        # /exception-down in the debugger.
        raise RuntimeError(f"shipping quote failed for zone {shipping_zone}") from error



def depth_9(cart: Cart, quote: ShippingQuote, zone_divisors: dict) -> None:
    divisor = zone_divisors[quote.zone]
    depth_10(cart, quote, divisor)




def depth_10(cart: Cart, quote: ShippingQuote, divisor: int) -> None:
    customer_name = cart.customer.name
    print(f"Calculating shipping for {customer_name}...")
    quote.cost = quote.discounted_total / divisor  # Intentional exception.
    print(f"Shipping cost: {quote.cost}")


# -- a second, unrelated failure ---------------------------------------------


def parse_order(payload: dict) -> LineItem:
    """A bad payload fails validation, which is its own exception path."""
    try:
        return LineItem(**payload)
    except ValidationError as error:
        # No decorator here: capture straight from the handler. ``error`` is a
        # local of this frame, so error.errors() is inspectable in the dump.
        trace_id = dump_exception(metadata={"payload": payload})
        report(trace_id)
        raise


def main() -> None:
    try:
        parse_order({"sku": "BROKEN-4", "price": -3.0, "quantity": 0})
    except ValidationError:
        pass
    depth_1()


if __name__ == "__main__":
    main()
